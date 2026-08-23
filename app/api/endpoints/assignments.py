"""QA assignments API endpoints.

A Supervisor/Admin wires a QA to review targets (see
``app.models.assignment``):

- General: ``support_agent_id`` set — the QA reviews that one agent;
- Specialized: ``specialized_case_type`` set — the QA reviews that case
  type across all agents;
- Hybrid: both set — the QA is scoped to that agent AND that case type.

The assignment rows drive the QA compliance report
(``GET /api/reviews/quota-compliance/{qa_id}``): each assigned agent
owes one review per pacing interval.

RBAC matrix:
- GET /api/assignments: Supervisor/Admin only — QA and Support-only
  users get 403 from the RoleChecker.
- POST /api/assignments: Supervisor/Admin only. 400 when the target
  ``qa_id`` user does not exist or holds no QA role, or when a provided
  ``support_agent_id`` does not exist or holds no Support role; 409 on
  an exact duplicate (same qa_id + support_agent_id +
  specialized_case_type combination, NULLs compared as equal).
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
    """Resolve a payload user reference; 400 unknown or wrong role.

    Both failure modes are client errors about the same invalid
    reference, so they share one status code (unlike review targets,
    where an unknown agent is a 404).
    """
    user = await db.get(User, user_id)
    if user is None:
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
    """Create a General/Specialized/Hybrid QA assignment.

    - 400 when ``qa_id`` does not exist or holds no 'QA' role, or when
      ``support_agent_id`` is provided but does not exist or holds no
      'Support' role.
    - 409 when an identical assignment already exists: same
      ``qa_id`` + ``support_agent_id`` + ``specialized_case_type``
      combination with NULLs treated consistently (a General assignment
      to agent X only blocks other General assignments to X — a
      Specialized or Hybrid row remains creatable).
    """
    await _get_user_with_role_or_400(
        payload.qa_id, RoleEnum.QA, label="Assigned QA", db=db
    )
    if payload.support_agent_id is not None:
        await _get_user_with_role_or_400(
            payload.support_agent_id,
            RoleEnum.SUPPORT,
            label="Support agent",
            db=db,
        )

    # Exact-duplicate guard. Column == None renders IS NULL in SQL, but
    # branch explicitly so both sides always compare the same way.
    conditions = [QAAssignment.qa_id == payload.qa_id]
    if payload.support_agent_id is None:
        conditions.append(QAAssignment.support_agent_id.is_(None))
    else:
        conditions.append(
            QAAssignment.support_agent_id == payload.support_agent_id
        )
    if payload.specialized_case_type is None:
        conditions.append(QAAssignment.specialized_case_type.is_(None))
    else:
        conditions.append(
            QAAssignment.specialized_case_type == payload.specialized_case_type
        )
    duplicate = await db.scalar(select(QAAssignment).where(*conditions))
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "An identical assignment already exists "
                    f"(qa_id={payload.qa_id}, "
                    f"support_agent_id={payload.support_agent_id}, "
                    "specialized_case_type="
                    f"{payload.specialized_case_type.value if payload.specialized_case_type else None})."
                ),
                "assignment_id": duplicate.id,
            },
        )

    assignment = QAAssignment(
        qa_id=payload.qa_id,
        support_agent_id=payload.support_agent_id,
        specialized_case_type=payload.specialized_case_type,
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
