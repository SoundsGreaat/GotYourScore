"""Shared Bad Feedback and Refund Check workflow endpoints.

Both trackers use the same handlers and lifecycle, but each router is
bound to its own ORM tables and stable tracker-field configuration.
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

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import (
    AgentKindEnum,
    BadFeedback,
    BadFeedbackAgent,
    FaultEnum,
    RefundCheck,
    RefundCheckAgent,
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
    RefundCheckAgentRead,
    RefundCheckCreate,
    RefundCheckImportCommitResponse,
    RefundCheckImportInspectResponse,
    RefundCheckRead,
    RefundCheckUpdate,
)
from app.services import bad_feedback_import as bfi
from app.services.tracker_fields import (
    DATE_FIELD_KEYS,
    apply_field_values,
    canonical_field_keys,
    list_fields,
    lock_field_configuration,
    validate_field_values,
)

DbSession = Annotated[AsyncSession, Depends(get_db)]

# QA-and-above manage records; front-line roles read nothing here.
FeedbackUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]

_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_LIST_LIMIT = 500


async def _get_record_or_404(
    feedback_id: int,
    db: AsyncSession,
    model: type,
    display_name: str,
    *,
    for_write: bool = False,
):
    """Resolve a non-deleted record; 404 otherwise."""
    # Serialize edits before reading the JSONB object that PATCH will merge.
    record = await db.get(model, feedback_id, with_for_update=for_write or None)
    if record is None or record.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{display_name} record {feedback_id} not found.",
        )
    return record


async def _get_agent_user_or_error(user_id: int, db: AsyncSession) -> User:
    """Resolve an agent card's user; 400 unknown/deleted."""
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent user {user_id} not found.",
        )
    return user


def _agent_read(agent, schema: type[BadFeedbackAgentRead]):
    """Serialize an agent card with its display label resolved."""
    return schema(
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


def _read_record(
    record: BadFeedback | RefundCheck,
    read_schema: type[BadFeedbackRead] | type[RefundCheckRead],
    agent_read_schema: type[BadFeedbackAgentRead],
    date_key: str,
):
    """Serialize a record and its agent cards."""
    return read_schema(
        id=record.id,
        source=record.source,
        customer_info=record.customer_info,
        customer_feedback=record.customer_feedback,
        related_case=record.related_case,
        field_values=record.field_values,
        qa_comment=record.qa_comment,
        status=record.status,
        assigned_qa_id=record.assigned_qa_id,
        qa_id=record.qa_id,
        completed_at=record.completed_at,
        created_by=record.created_by,
        created_at=record.created_at,
        deleted_at=record.deleted_at,
        agents=[_agent_read(agent, agent_read_schema) for agent in record.agents],
        **{date_key: getattr(record, date_key)},
    )


async def _validate_agents_payload(
    agents: list[BadFeedbackAgentIn] | None,
    db: AsyncSession,
) -> list[tuple[User, AgentKindEnum, FaultEnum | None, str | None]] | None:
    """Resolve every agent card's user and reject duplicate kind/user pairs."""
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
    record,
    resolved: list[tuple[User, AgentKindEnum, FaultEnum | None, str | None]],
    db: AsyncSession,
    agent_model: type,
) -> None:
    """Replace agent cards by (user_id, kind), preserving surviving rows."""
    existing = {(agent.user_id, agent.kind): agent for agent in record.agents}
    next_pairs: set[tuple[int, AgentKindEnum]] = set()
    for user, kind, fault, comment in resolved:
        pair = (user.id, kind)
        next_pairs.add(pair)
        card = existing.get(pair)
        if card is None:
            record.agents.append(
                agent_model(user=user, kind=kind, fault=fault, qa_comment=comment)
            )
        else:
            card.fault = fault
            card.qa_comment = comment
    for pair, card in existing.items():
        if pair not in next_pairs:
            record.agents.remove(card)


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


async def _apply_dynamic_values(
    record, values: dict[str, str | None], tracker: str, db: AsyncSession
) -> None:
    try:
        validated = await validate_field_values(db, tracker, values)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    apply_field_values(record, validated)


def _tracker_router(
    *,
    tracker: str,
    display_name: str,
    record_model: type,
    agent_model: type,
    agent_read_schema: type,
    create_schema: type,
    update_schema: type,
    read_schema: type,
    inspect_schema: type,
    commit_schema: type,
) -> APIRouter:
    """Bind shared route handlers to one independent tracker."""
    child = APIRouter(prefix=f"/{tracker}", tags=[tracker])
    date_key = DATE_FIELD_KEYS[tracker]
    flat_keys = canonical_field_keys(tracker)

    async def list_records(auth, db, status_filter=None, limit=200, offset=0):
        if limit > _MAX_LIST_LIMIT:
            limit = _MAX_LIST_LIMIT
        stmt = select(record_model).where(record_model.deleted_at.is_(None))
        if status_filter in (
            ReviewStatusEnum.PENDING.value,
            ReviewStatusEnum.COMPLETED.value,
        ):
            stmt = stmt.where(record_model.status == status_filter)
        stmt = (
            stmt.order_by(record_model.created_at.desc(), record_model.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await db.execute(stmt)).scalars().unique().all()
        return [
            _read_record(record, read_schema, agent_read_schema, date_key)
            for record in rows
        ]

    list_records.__annotations__ = {
        "auth": FeedbackUser,
        "db": DbSession,
        "status_filter": str | None,
        "limit": int,
        "offset": int,
        "return": list[read_schema],
    }
    child.add_api_route(
        "",
        list_records,
        methods=["GET"],
        response_model=list[read_schema],
        name=f"list_{tracker}_records",
    )

    async def create_record(payload, auth, db):
        resolved = await _validate_agents_payload(payload.agents, db)
        await _validate_assigned_qa(payload.assigned_qa_id, db)
        legacy_values = payload.model_dump(
            include=flat_keys,
            mode="json",
            exclude_unset=True,
        )
        incoming_values = {**legacy_values, **payload.field_values}
        record = record_model(
            status=(
                ReviewStatusEnum.COMPLETED
                if payload.complete
                else ReviewStatusEnum.PENDING
            ),
            assigned_qa_id=payload.assigned_qa_id,
            created_by=auth.id,
            # Keep the collection initialized even for an empty draft so
            # response serialization never performs an async lazy load.
            agents=[],
        )
        # Normalize legacy flat values through the same active-field/type
        # validation path. Explicit field_values has precedence for duplicate
        # keys while the canonical top-level contract remains compatible.
        if incoming_values:
            await _apply_dynamic_values(record, incoming_values, tracker, db)
        if payload.complete:
            record.qa_id = auth.id
            record.completed_at = datetime.now(timezone.utc)
        if resolved:
            await _apply_agents(record, resolved, db, agent_model)
        db.add(record)
        await db.commit()
        return _read_record(record, read_schema, agent_read_schema, date_key)

    create_record.__annotations__ = {
        "payload": create_schema,
        "auth": FeedbackUser,
        "db": DbSession,
        "return": read_schema,
    }
    child.add_api_route(
        "",
        create_record,
        methods=["POST"],
        response_model=read_schema,
        status_code=status.HTTP_201_CREATED,
        name=f"create_{tracker}_record",
    )

    async def get_record(feedback_id, auth, db):
        record = await _get_record_or_404(
            feedback_id, db, record_model, display_name
        )
        return _read_record(record, read_schema, agent_read_schema, date_key)

    get_record.__annotations__ = {
        "feedback_id": int,
        "auth": FeedbackUser,
        "db": DbSession,
        "return": read_schema,
    }
    child.add_api_route(
        "/{feedback_id}",
        get_record,
        methods=["GET"],
        response_model=read_schema,
        name=f"get_{tracker}_record",
    )

    async def update_record(feedback_id, payload, auth, db):
        record = await _get_record_or_404(
            feedback_id, db, record_model, display_name, for_write=True
        )
        data = payload.model_dump(exclude_unset=True)
        legacy_values = payload.model_dump(
            include=flat_keys,
            mode="json",
            exclude_unset=True,
        )

        if "assigned_qa_id" in data:
            await _validate_assigned_qa(data["assigned_qa_id"], db)
            record.assigned_qa_id = data["assigned_qa_id"]

        incoming_values = dict(legacy_values)
        if "field_values" in data:
            incoming_values.update(data["field_values"])
        # Both legacy and dynamic inputs must target active configured fields
        # and obey their current types; field_values wins for duplicate keys.
        if incoming_values:
            await _apply_dynamic_values(record, incoming_values, tracker, db)

        if payload.agents is not None:
            resolved = await _validate_agents_payload(payload.agents, db)
            await _apply_agents(record, resolved, db, agent_model)

        await db.commit()
        return _read_record(record, read_schema, agent_read_schema, date_key)

    update_record.__annotations__ = {
        "feedback_id": int,
        "payload": update_schema,
        "auth": FeedbackUser,
        "db": DbSession,
        "return": read_schema,
    }
    child.add_api_route(
        "/{feedback_id}",
        update_record,
        methods=["PATCH"],
        response_model=read_schema,
        name=f"update_{tracker}_record",
    )

    async def complete_record(feedback_id, auth, db):
        record = await _get_record_or_404(
            feedback_id, db, record_model, display_name, for_write=True
        )
        if record.status is ReviewStatusEnum.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{display_name} record is already completed.",
            )
        record.status = ReviewStatusEnum.COMPLETED
        record.qa_id = auth.id
        record.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return _read_record(record, read_schema, agent_read_schema, date_key)

    complete_record.__annotations__ = {
        "feedback_id": int,
        "auth": FeedbackUser,
        "db": DbSession,
        "return": read_schema,
    }
    child.add_api_route(
        "/{feedback_id}/complete",
        complete_record,
        methods=["POST"],
        response_model=read_schema,
        name=f"complete_{tracker}_record",
    )

    async def delete_record(feedback_id, auth, db):
        record = await _get_record_or_404(
            feedback_id, db, record_model, display_name, for_write=True
        )
        record.deleted_at = func.now()
        await db.commit()

    delete_record.__annotations__ = {
        "feedback_id": int,
        "auth": FeedbackUser,
        "db": DbSession,
    }
    child.add_api_route(
        "/{feedback_id}",
        delete_record,
        methods=["DELETE"],
        status_code=status.HTTP_204_NO_CONTENT,
        name=f"delete_{tracker}_record",
    )

    async def import_inspect(auth, db, file):
        content = await file.read()
        if len(content) > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File is larger than 5 MB.",
            )
        try:
            fields = await list_fields(db, tracker)
            return bfi.inspect_workbook(
                content,
                fields,
                include_legacy_date_alias=tracker == "bad-feedback",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not read the file as an .xlsx workbook.",
            ) from exc

    import_inspect.__annotations__ = {
        "auth": FeedbackUser,
        "db": DbSession,
        "file": Annotated[UploadFile, File()],
        "return": inspect_schema,
    }
    child.add_api_route(
        "/import/inspect",
        import_inspect,
        methods=["POST"],
        response_model=inspect_schema,
        name=f"inspect_{tracker}_import",
    )

    async def import_commit(auth, db, file, mapping, excluded_rows=None, assigned_qa_id=None):
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
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mapping must be a JSON object of field -> header.",
            ) from exc
        try:
            excluded_raw = json.loads(excluded_rows or "[]")
            if not isinstance(excluded_raw, list) or any(
                isinstance(row, bool) or not isinstance(row, int) or row < 2
                for row in excluded_raw
            ):
                raise ValueError
            excluded_row_numbers = set(excluded_raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="excluded_rows must be a JSON array of spreadsheet row numbers.",
            ) from exc
        if assigned_qa_id is not None:
            await _validate_assigned_qa(assigned_qa_id, db)
        try:
            await lock_field_configuration(db, tracker, shared=True)
            fields = await list_fields(db, tracker)
            return await bfi.commit_import(
                db=db,
                content=content,
                mapping=mapping_dict,
                excluded_rows=excluded_row_numbers,
                assigned_qa_id=assigned_qa_id,
                created_by=auth.id,
                tracker=tracker,
                fields=fields,
                include_legacy_date_alias=tracker == "bad-feedback",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not read the file as an .xlsx workbook.",
            ) from exc

    import_commit.__annotations__ = {
        "auth": FeedbackUser,
        "db": DbSession,
        "file": Annotated[UploadFile, File()],
        "mapping": Annotated[str, Form()],
        "excluded_rows": Annotated[str | None, Form()],
        "assigned_qa_id": Annotated[int | None, Form()],
        "return": commit_schema,
    }
    child.add_api_route(
        "/import",
        import_commit,
        methods=["POST"],
        response_model=commit_schema,
        name=f"commit_{tracker}_import",
    )
    return child


router = _tracker_router(
    tracker="bad-feedback",
    display_name="Bad Feedback",
    record_model=BadFeedback,
    agent_model=BadFeedbackAgent,
    agent_read_schema=BadFeedbackAgentRead,
    create_schema=BadFeedbackCreate,
    update_schema=BadFeedbackUpdate,
    read_schema=BadFeedbackRead,
    inspect_schema=ImportInspectResponse,
    commit_schema=ImportCommitResponse,
)

refund_check_router = _tracker_router(
    tracker="refund-check",
    display_name="Refund Check",
    record_model=RefundCheck,
    agent_model=RefundCheckAgent,
    agent_read_schema=RefundCheckAgentRead,
    create_schema=RefundCheckCreate,
    update_schema=RefundCheckUpdate,
    read_schema=RefundCheckRead,
    inspect_schema=RefundCheckImportInspectResponse,
    commit_schema=RefundCheckImportCommitResponse,
)
