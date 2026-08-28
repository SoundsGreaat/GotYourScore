"""Bad Feedback endpoints: list, create, edit, complete, smart import.

Lifecycle mirrors Review delegation: records arrive as ``pending``
(via import or manual create), optionally routed to a named QA
(``assigned_qa_id``; null = shared queue), and are finished by an
explicit complete action that stamps ``qa_id`` with the finisher.

Import is two-phase and stateless: inspect (parse + suggest) then
commit (re-upload + confirmed mapping). Unknown agent names become
placeholder users — see ``app.services.bad_feedback_import``.
"""

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker, get_current_user
from app.db.database import get_db
from app.models import (
    AgentKindEnum,
    BadFeedback,
    BadFeedbackAgent,
    FaultEnum,
    ReviewStatusEnum,
    RoleEnum,
    User,
)
from app.schemas.bad_feedback import (
    BadFeedbackAgentIn,
    BadFeedbackAgentRead,
    BadFeedbackCreate,
    BadFeedbackRead,
    BadFeedbackUpdate,
    ImportCommitResponse,
    ImportInspectResponse,
)
from app.services import bad_feedback_import as bfi

router = APIRouter(prefix="/bad-feedback", tags=["bad-feedback"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# QA-and-above manage records; front-line roles read nothing here.
FeedbackUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]

_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_LIST_LIMIT = 500


async def _get_feedback_or_404(feedback_id: int, db: AsyncSession) -> BadFeedback:
    """Resolve a non-deleted record; 404 otherwise."""
    feedback = await db.get(BadFeedback, feedback_id)
    if feedback is None or feedback.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bad Feedback record {feedback_id} not found.",
        )
    return feedback


async def _get_agent_user_or_error(user_id: int, db: AsyncSession) -> User:
    """Resolve an agent card's user; 400 unknown/deleted."""
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent user {user_id} not found.",
        )
    return user


def _agent_read(agent: BadFeedbackAgent) -> BadFeedbackAgentRead:
    """Serialize an agent card with its display label resolved."""
    return BadFeedbackAgentRead(
        id=agent.id,
        user_id=agent.user_id,
        kind=agent.kind,
        fault=agent.fault,
        qa_comment=agent.qa_comment,
        user_label=(
            agent.user.name
            if agent.user is not None and agent.user.name
            else agent.user.nickname if agent.user is not None else None
        ),
    )


def _read(feedback: BadFeedback) -> BadFeedbackRead:
    """Serialize a record with agent cards (labels resolved eagerly)."""
    return BadFeedbackRead(
        id=feedback.id,
        fb_date=feedback.fb_date,
        source=feedback.source,
        customer_info=feedback.customer_info,
        customer_feedback=feedback.customer_feedback,
        related_case=feedback.related_case,
        qa_comment=feedback.qa_comment,
        status=feedback.status,
        assigned_qa_id=feedback.assigned_qa_id,
        qa_id=feedback.qa_id,
        completed_at=feedback.completed_at,
        created_by=feedback.created_by,
        created_at=feedback.created_at,
        deleted_at=feedback.deleted_at,
        agents=[_agent_read(a) for a in feedback.agents],
    )


async def _validate_agents_payload(
    agents: list[BadFeedbackAgentIn] | None,
    db: AsyncSession,
) -> list[tuple[User, AgentKindEnum, FaultEnum | None, str | None]] | None:
    """Resolve every agent card's user; returns None when absent.

    Duplicate (user_id, kind) pairs are rejected — the modal list is
    de-duped client-side, but a stale tab could submit doubles.
    """
    if agents is None:
        return None
    resolved: list[tuple[User, AgentKindEnum, FaultEnum | None, str | None]] = []
    seen: set[tuple[int, AgentKindEnum]] = set()
    for item in agents:
        user = await _get_agent_user_or_error(item.user_id, db)
        pair = (user.id, item.kind)
        if pair in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Agent {user.nickname} listed twice as "
                    f"{item.kind.value}."
                ),
            )
        seen.add(pair)
        resolved.append((user, item.kind, item.fault, item.qa_comment))
    return resolved


async def _apply_agents(
    feedback: BadFeedback,
    resolved: list[tuple[User, AgentKindEnum, FaultEnum | None, str | None]],
    db: AsyncSession,
) -> None:
    """Replace the record's agent cards with the payload's list.

    Sync by (user_id, kind): surviving cards keep their fault/comment
    edits; removed cards disappear; new cards start with the payload's
    fault/comment.
    """
    existing = {
        (a.user_id, a.kind): a for a in feedback.agents
    }
    next_pairs: set[tuple[int, AgentKindEnum]] = set()
    for user, kind, fault, comment in resolved:
        pair = (user.id, kind)
        next_pairs.add(pair)
        card = existing.get(pair)
        if card is None:
            feedback.agents.append(
                BadFeedbackAgent(user=user, kind=kind, fault=fault, qa_comment=comment)
            )
        else:
            card.fault = fault
            card.qa_comment = comment
    for pair, card in existing.items():
        if pair not in next_pairs:
            feedback.agents.remove(card)


async def _validate_assigned_qa(
    assigned_qa_id: int | None, db: AsyncSession
) -> None:
    """400 when a provided assignee is unknown/deleted or not a QA."""
    if assigned_qa_id is None:
        return
    qa = await db.get(User, assigned_qa_id)
    if qa is None or qa.deleted_at is not None or not qa.has_role(RoleEnum.QA):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Assigned QA {assigned_qa_id} not found or has no QA role.",
        )


@router.get("", response_model=list[BadFeedbackRead])
async def list_feedback(
    auth: FeedbackUser,
    db: DbSession,
    status_filter: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[BadFeedbackRead]:
    """List records newest-first (QA/Supervisor/Admin only).

    ``status_filter``: 'pending' | 'completed' (default: both).
    """
    if limit > _MAX_LIST_LIMIT:
        limit = _MAX_LIST_LIMIT
    stmt = select(BadFeedback).where(BadFeedback.deleted_at.is_(None))
    if status_filter in (ReviewStatusEnum.PENDING.value, ReviewStatusEnum.COMPLETED.value):
        stmt = stmt.where(BadFeedback.status == status_filter)
    stmt = (
        stmt.order_by(BadFeedback.created_at.desc(), BadFeedback.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().unique().all()
    return [_read(fb) for fb in rows]


@router.post("", response_model=BadFeedbackRead, status_code=201)
async def create_feedback(
    payload: BadFeedbackCreate,
    auth: FeedbackUser,
    db: DbSession,
) -> BadFeedbackRead:
    """Manually create one record (QA/Supervisor/Admin).

    Pending unless ``complete`` is set (New Bad Feedback modal's
    "Save"): then the creator finishes the record in the same request —
    status completed, ``qa_id`` = caller, ``completed_at`` stamped.
    """
    resolved = await _validate_agents_payload(payload.agents, db)
    await _validate_assigned_qa(payload.assigned_qa_id, db)
    feedback = BadFeedback(
        fb_date=payload.fb_date,
        source=payload.source,
        customer_info=payload.customer_info,
        customer_feedback=payload.customer_feedback,
        related_case=payload.related_case,
        status=(
            ReviewStatusEnum.COMPLETED
            if payload.complete
            else ReviewStatusEnum.PENDING
        ),
        assigned_qa_id=payload.assigned_qa_id,
        created_by=auth.id,
    )
    if payload.complete:
        feedback.qa_id = auth.id
        feedback.completed_at = datetime.now(timezone.utc)
    if resolved:
        await _apply_agents(feedback, resolved, db)
    db.add(feedback)
    await db.commit()
    return _read(feedback)


@router.get("/{feedback_id}", response_model=BadFeedbackRead)
async def get_feedback(
    feedback_id: int,
    auth: FeedbackUser,
    db: DbSession,
) -> BadFeedbackRead:
    """Fetch one record with agent cards."""
    feedback = await _get_feedback_or_404(feedback_id, db)
    return _read(feedback)


@router.patch("/{feedback_id}", response_model=BadFeedbackRead)
async def update_feedback(
    feedback_id: int,
    payload: BadFeedbackUpdate,
    auth: FeedbackUser,
    db: DbSession,
) -> BadFeedbackRead:
    """Edit a record (any QA+).

    ``agents`` (when present) is REPLACE-ALL synced by (user_id, kind).
    ``assigned_qa_id`` is tri-state via ``model_fields_set``: explicit
    null clears the assignment back to the shared queue. Editing is
    allowed regardless of status so post-completion corrections stay
    possible; ``status``/``qa_id`` are never editable here.
    """
    feedback = await _get_feedback_or_404(feedback_id, db)
    data = payload.model_dump(exclude_unset=True)

    if "assigned_qa_id" in data:
        await _validate_assigned_qa(data["assigned_qa_id"], db)
        feedback.assigned_qa_id = data["assigned_qa_id"]

    for key in ("fb_date", "source", "customer_info", "customer_feedback", "related_case"):
        if key in data:
            setattr(feedback, key, data[key])

    if payload.agents is not None:
        resolved = await _validate_agents_payload(payload.agents, db)
        await _apply_agents(feedback, resolved, db)

    await db.commit()
    return _read(feedback)


@router.post("/{feedback_id}/complete", response_model=BadFeedbackRead)
async def complete_feedback(
    feedback_id: int,
    auth: FeedbackUser,
    db: DbSession,
) -> BadFeedbackRead:
    """Finish a record: status -> completed, ``qa_id`` = finisher."""
    feedback = await _get_feedback_or_404(feedback_id, db)
    if feedback.status is ReviewStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bad Feedback record is already completed.",
        )
    feedback.status = ReviewStatusEnum.COMPLETED
    feedback.qa_id = auth.id
    feedback.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return _read(feedback)


@router.delete("/{feedback_id}", status_code=204)
async def delete_feedback(
    feedback_id: int,
    auth: FeedbackUser,
    db: DbSession,
) -> None:
    """Soft-delete a record (QA/Supervisor/Admin)."""
    feedback = await _get_feedback_or_404(feedback_id, db)
    feedback.deleted_at = func.now()
    await db.commit()


@router.post("/import/inspect", response_model=ImportInspectResponse)
async def import_inspect(
    auth: FeedbackUser,
    file: Annotated[UploadFile, File()],
) -> ImportInspectResponse:
    """Parse the uploaded xlsx: headers + preview + mapping suggestions.

    Nothing is persisted — the client re-uploads the same file for the
    commit call after the user confirms the mapping.
    """
    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is larger than 5 MB.",
        )
    try:
        return bfi.inspect_workbook(content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read the file as an .xlsx workbook.",
        )


@router.post("/import", response_model=ImportCommitResponse)
async def import_commit(
    auth: FeedbackUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str, Form()],
    assigned_qa_id: Annotated[int | None, Form()] = None,
) -> ImportCommitResponse:
    """Commit the import with the user-confirmed column mapping.

    ``mapping`` is JSON: canonical field key -> header text (or null to
    leave the field unmapped). Records land as pending; unknown agent
    names are created as placeholder users with Support/Sales roles.
    """
    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is larger than 5 MB.",
        )
    try:
        mapping_dict = json.loads(mapping)
        if not isinstance(mapping_dict, dict):
            raise ValueError("mapping must be a JSON object")
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mapping must be a JSON object of field -> header.",
        )
    if assigned_qa_id is not None:
        await _validate_assigned_qa(assigned_qa_id, db)
    try:
        return await bfi.commit_import(
            db=db,
            content=content,
            mapping=mapping_dict,
            assigned_qa_id=assigned_qa_id,
            created_by=auth.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read the file as an .xlsx workbook.",
        )
