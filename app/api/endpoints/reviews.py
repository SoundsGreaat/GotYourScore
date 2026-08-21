"""Reviews API endpoints.

- POST /api/reviews: create a review (progressive multiplier scoring,
  monthly quota enforcement, 'No Cases' edge case).
- GET /api/reviews/quota/{agent_id}: quota status for the current
  (or explicitly requested) month.
- GET /api/reviews/{review_id}: fetch a single review.

All routes require an authenticated user with the QA, Supervisor or
Admin role.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import CaseTypeEnum, Review, RoleEnum, User
from app.schemas.review import QuotaRead, ReviewCreate, ReviewRead
from app.services import multiplier_service, quota_service

router = APIRouter(prefix="/reviews", tags=["reviews"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# RoleChecker returns the authenticated User, so the handler receives it.
ReviewerUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.post("", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
async def create_review(
    payload: ReviewCreate,
    db: DbSession,
    current_user: ReviewerUser,
) -> ReviewRead:
    """Create a review for a support agent.

    - 404 if the target user does not exist; 400 if it is not a Support
      agent.
    - 409 if the agent already reached the monthly quota
      (``completed >= target`` reviews this month).
    - ``case_type='No Cases'`` skips the math entirely: null
      scorecard_data and null final_score (still counts towards quota).
    - ``qa_id`` is injected from the authenticated caller.
    """
    agent = await db.get(User, payload.support_agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support agent {payload.support_agent_id} not found.",
        )
    if agent.role is not RoleEnum.SUPPORT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"User {agent.id} has role '{agent.role.value}' but reviews "
                "can only target users with role 'Support'."
            ),
        )

    # Quota check for the current month BEFORE doing any math.
    # Not race-free: two concurrent POSTs can both pass the check; use
    # pg_advisory_xact_lock(agent_id) around check+insert if strictness
    # is ever required.
    now = _utcnow()
    quota = await quota_service.get_agent_quota(agent.id, now.year, now.month, db)
    if quota["completed"] >= quota["target"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Monthly quota reached for support agent {agent.id}: "
                    f"{quota['completed']}/{quota['target']} reviews this month."
                ),
                "completed": quota["completed"],
                "target": quota["target"],
            },
        )

    if payload.case_type is CaseTypeEnum.NO_CASES:
        scorecard_data: dict[str, dict[str, int]] | None = None
        final_score: int | None = None
    else:
        scorecard_data, final_score = await multiplier_service.calculate_final_score(
            agent.id, payload.raw_scorecard or {}, db
        )

    review = Review(
        support_agent_id=agent.id,
        qa_id=current_user.id,
        case_type=payload.case_type,
        scorecard_data=scorecard_data,
        notes=payload.notes,
        final_score=final_score,
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return ReviewRead.model_validate(review)


@router.get("/quota/{agent_id}", response_model=QuotaRead)
async def get_agent_quota(
    agent_id: int,
    db: DbSession,
    _current_user: ReviewerUser,
    year: int | None = Query(default=None, ge=1900, le=3000),
    month: int | None = Query(default=None, ge=1, le=12),
) -> QuotaRead:
    """Quota status for the current month (or the given year/month)."""
    agent = await db.get(User, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support agent {agent_id} not found.",
        )

    now = _utcnow()
    quota = await quota_service.get_agent_quota(
        agent_id,
        year if year is not None else now.year,
        month if month is not None else now.month,
        db,
    )
    return QuotaRead(**quota)


@router.get("/{review_id}", response_model=ReviewRead)
async def get_review(
    review_id: int,
    db: DbSession,
    _current_user: ReviewerUser,
) -> ReviewRead:
    """Fetch a single review by id (404 if it does not exist)."""
    review = await db.get(Review, review_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review {review_id} not found.",
        )
    return ReviewRead.model_validate(review)
