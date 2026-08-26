"""QA assignments API endpoints.

A Supervisor/Admin staffs a Support agent to a QA reviewer (see
``app.models.assignment``): the assigned QA owns that agent's
reporting-period quota.

The assignment rows drive the QA compliance report
(``GET /api/reviews/quota-compliance/{qa_id}``): each assigned agent
owes one review per pacing interval.

RBAC matrix:
- GET /api/assignments: Supervisor/Admin only — QA and Support-only
  users get 403 from the RoleChecker.
- POST /api/assignments: Supervisor/Admin only. 400 when the target
  ``qa_id`` user does not exist or holds no QA role, or when the
  ``support_agent_id`` does not exist or holds no Support role; 409
  when that agent is already staffed to ANY QA (an agent works with at
  most one QA; moving them is delete + recreate).
- DELETE /api/assignments/{assignment_id}: Supervisor/Admin only; 404
  for unknown ids.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import QAAssignment, RoleEnum, User
from app.schemas.assignment import QAAssignmentCreate, QAAssignmentRead

router = APIRouter(prefix="/assignments", tags=["assignments"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# Assignments are a staffing decision — never exposed to QAs themselves.
SupervisorAdminUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]


async def _get_user_with_role_or_400(
    user_id: int, role: RoleEnum, *, label: str, db: AsyncSession
) -> User:
    """Resolve a payload user reference; 400 unknown/deleted/wrong role.

    All failure modes are client errors about the same invalid
    reference, so they share one status code (unlike review targets,
    where an unknown agent is a 404).
    """
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} user {user_id} not found.",
        )
    if not user.has_role(role):
        roles = ", ".join(r.value for r in user.roles) or "none"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"User {user_id} has roles '{roles}' but assignments "
                f"require the '{role.value}' role for the {label.lower()}."
            ),
        )
    return user


@router.get("", response_model=list[QAAssignmentRead])
async def list_assignments(
    db: DbSession,
    _current_user: SupervisorAdminUser,
) -> list[QAAssignmentRead]:
    """List every QA assignment, ordered by ``qa_id`` then ``created_at``.

    RBAC: Supervisor/Admin only (403 otherwise).
    """
    result = await db.execute(
        select(QAAssignment).order_by(QAAssignment.qa_id, QAAssignment.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "", response_model=QAAssignmentRead, status_code=status.HTTP_201_CREATED
)
async def create_assignment(
    payload: QAAssignmentCreate,
    db: DbSession,
    _current_user: SupervisorAdminUser,
) -> QAAssignmentRead:
    """Create a QA assignment.

    - 400 when ``qa_id`` does not exist or holds no 'QA' role, or when
      ``support_agent_id`` does not exist or holds no 'Support' role.
    - 409 when the agent is already staffed to a QA (an agent works
      with at most one QA; the DB enforces this via UNIQUE too).
    """
    await _get_user_with_role_or_400(
        payload.qa_id, RoleEnum.QA, label="Assigned QA", db=db
    )
    support_agent = await _get_user_with_role_or_400(
        payload.support_agent_id,
        RoleEnum.SUPPORT,
        label="Support agent",
        db=db,
    )

    # One-agent-one-QA guard: any existing row for this agent blocks
    # creation, not just an identical pair. The UNIQUE constraint is
    # the backstop; resolving the holder here yields a clear message.
    existing = (
        await db.execute(
            select(QAAssignment).where(
                QAAssignment.support_agent_id == payload.support_agent_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        holder = await db.get(User, existing.qa_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Support agent {support_agent.nickname} is already "
                    f"assigned to QA {holder.nickname if holder else existing.qa_id}."
                ),
                "assignment_id": existing.id,
            },
        )

    assignment = QAAssignment(
        qa_id=payload.qa_id,
        support_agent_id=payload.support_agent_id,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return QAAssignmentRead.model_validate(assignment)


@router.delete(
    "/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_assignment(
    assignment_id: int,
    db: DbSession,
    _current_user: SupervisorAdminUser,
) -> None:
    """Delete an assignment outright (assignments are staffing metadata,
    not audit records — hard delete).

    RBAC: Supervisor/Admin only. 404 for unknown ids.
    """
    assignment = await db.get(QAAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assignment {assignment_id} not found.",
        )
    await db.delete(assignment)
    await db.commit()
