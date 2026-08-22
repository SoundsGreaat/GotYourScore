"""Reviews API endpoints.

- POST /api/reviews: create a review (rule snapshot embedded into
  scorecard_data, progressive multiplier scoring, reporting-period
  quota enforcement, 'No Cases' edge case).
- POST /api/reviews/auto-score: create a review from an AI-analyzed
  ticket transcript (OpenRouter; 503 when unconfigured, 502 when the
  analysis fails).
- POST /api/reviews/score-preview: read-only preview of the breakdown
  a save would produce for a raw scorecard (nothing persisted).
- GET /api/reviews/quota/{agent_id}: quota status for the current (or
  explicitly requested) REPORTING period.
- GET /api/reviews/quota-compliance/{qa_id}: per-interval quota
  compliance for one QA over a reporting period.
- GET /api/reviews/{review_id}: fetch a single review.

RBAC matrix:
- POST endpoints: QA/Supervisor/Admin only — Support-only users get
  403 from the RoleChecker; a Support+QA hybrid passes and may review
  other Support agents.
- GET /quota/{agent_id}: reviewer roles unrestricted; Support-only
  users may query only their own agent_id (403 otherwise).
- GET /quota-compliance/{qa_id}: Admin/Supervisor may query any qa_id;
  a QA only their own qa_id (403 otherwise); Support-only always 403.
- GET /{review_id}: reviewer roles see everything; Support-only users
  may fetch only reviews where they are the reviewed agent — accessing
  another agent's review is an explicit 403 (never a 404).
"""

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker, get_current_user, is_reviewer
from app.db.database import get_db
from app.models import CaseTypeEnum, Review, RoleEnum, User
from app.schemas.review import (
    AutoScoreCreate,
    IntervalComplianceRead,
    QuotaComplianceRead,
    QuotaRead,
    ReviewCreate,
    ReviewRead,
    ScorePreviewRequest,
    ScorePreviewResponse,
)
from app.services import (
    ai_service,
    multiplier_service,
    quota_service,
    reporting_period,
    scorecard_service,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# RoleChecker returns the authenticated User, so the handler receives it.
ReviewerUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]

# Plain authenticated user for endpoints where Support-only users have
# limited access instead of being blocked outright.
AuthUser = Annotated[User, Depends(get_current_user)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _current_closing_period() -> tuple[int, int]:
    """(closing_year, closing_month) of the reporting period covering now."""
    _, _, closing_year, closing_month = reporting_period.reporting_period_for(
        _utcnow()
    )
    return closing_year, closing_month


async def _get_support_agent_or_error(agent_id: int, db: AsyncSession) -> User:
    """Resolve the review target; 404 unknown, 400 non-Support."""
    agent = await db.get(User, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support agent {agent_id} not found.",
        )
    if not agent.has_role(RoleEnum.SUPPORT):
        roles = ", ".join(role.value for role in agent.roles) or "none"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"User {agent.id} has roles '{roles}' but reviews "
                "can only target users with role 'Support'."
            ),
        )
    return agent


async def _reject_quota_reached(agent: User, db: AsyncSession) -> None:
    """409 once the agent reached MONTHLY_QUOTA in the current period.

    Not race-free: two concurrent POSTs can both pass the check; use
    pg_advisory_xact_lock(agent_id) around check+insert if strictness
    is ever required.
    """
    closing_year, closing_month = _current_closing_period()
    quota = await quota_service.get_agent_quota(
        agent.id, closing_year, closing_month, db
    )
    if quota["completed"] >= quota["target"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Reporting-period quota reached for support agent "
                    f"{agent.id}: {quota['completed']}/{quota['target']} "
                    "reviews this period."
                ),
                "completed": quota["completed"],
                "target": quota["target"],
            },
        )


@router.post("", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
async def create_review(
    payload: ReviewCreate,
    db: DbSession,
    current_user: ReviewerUser,
) -> ReviewRead:
    """Create a review for a support agent.

    - 404 if the target user does not exist; 400 if it holds no
      Support role.
    - 409 if the agent already reached the reporting-period quota
      (``completed >= target`` reviews this period).
    - ``case_type='No Cases'`` skips the math entirely: null
      scorecard_data and null final_score (still counts towards quota).
    - Otherwise the ACTIVE scorecard rules are snapshotted into
      ``scorecard_data.rules_snapshot`` alongside the computed
      breakdown — historical immutability: later rule edits never
      rewrite saved reviews.
    - ``qa_id`` is injected from the authenticated caller.
    """
    agent = await _get_support_agent_or_error(payload.support_agent_id, db)
    await _reject_quota_reached(agent, db)

    if payload.case_type is CaseTypeEnum.NO_CASES:
        scorecard_data: dict[str, Any] | None = None
        final_score: int | None = None
    else:
        breakdown, final_score, total_penalty = (
            await multiplier_service.calculate_final_score(
                agent.id, payload.raw_scorecard or {}, db
            )
        )
        rules_snapshot = await scorecard_service.get_active_rules(
            payload.case_type, db
        )
        scorecard_data = {
            "rules_snapshot": rules_snapshot,
            "base_score": multiplier_service.BASE_SCORE,
            "total_penalty": total_penalty,
            "final_score": final_score,
            "breakdown": breakdown,
        }

    review = Review(
        support_agent_id=agent.id,
        qa_id=current_user.id,
        case_type=payload.case_type,
        case_number=payload.case_number,
        scorecard_data=scorecard_data,
        notes=payload.notes,
        final_score=final_score,
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return ReviewRead.model_validate(review)


@router.post("/score-preview", response_model=ScorePreviewResponse)
async def score_preview(
    payload: ScorePreviewRequest,
    db: DbSession,
    _current_user: ReviewerUser,
) -> ScorePreviewResponse:
    """Preview the multiplier-applied score for a raw scorecard.

    Read-only: identical math to what POST /api/reviews will compute
    at save time (same agent-occurrence scan, same base score), but
    nothing is inserted and the transaction is never committed.

    - 400 when ``case_type`` is not a valid CaseTypeEnum value or is
      'No Cases' (no scorecard to preview).
    - 404 if the target user does not exist; 400 if it holds no
      Support role (same messages as POST /api/reviews).
    """
    try:
        case_type = CaseTypeEnum(payload.case_type)
    except ValueError:
        valid = ", ".join(case.value for case in CaseTypeEnum)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown case type {payload.case_type!r}. Valid: {valid}."
            ),
        ) from None
    if case_type is CaseTypeEnum.NO_CASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "case_type 'No Cases' has no scorecard to preview "
                "(such reviews carry no scorecard data)."
            ),
        )

    # Same 404/400 contract and messages as create_review.
    agent = await _get_support_agent_or_error(payload.support_agent_id, db)

    breakdown, final_score, total_penalty = (
        await multiplier_service.calculate_final_score(
            agent.id, payload.raw_scorecard, db
        )
    )
    return ScorePreviewResponse(
        breakdown=breakdown,
        total_penalty=total_penalty,
        final_score=final_score,
    )


@router.post(
    "/auto-score",
    response_model=ReviewRead,
    status_code=status.HTTP_201_CREATED,
)
async def auto_score_review(
    payload: AutoScoreCreate,
    db: DbSession,
    current_user: ReviewerUser,
) -> ReviewRead:
    """Create a review from an AI-analyzed ticket transcript.

    The transcript is scored by the OpenRouter-hosted model (see
    ``app.services.ai_service``); the resulting raw scorecard then
    flows through the same progressive multiplier and reporting-period
    quota rules as a manual review, and the active rules are
    snapshotted into ``scorecard_data`` exactly like a manual review.

    - 404 if the target user does not exist; 400 if it holds no
      Support role; 409 if the reporting-period quota is already
      reached (a saved auto-scored review counts towards the quota,
      same business rule as POST /api/reviews).
    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI analysis call or response parsing fails —
      nothing is persisted in that case.
    - ``case_type`` defaults to SERVICE_REQUEST because
      ``reviews.case_type`` is NOT NULL and the auto-score flow has no
      explicit case type; callers may override it in the payload, but
      NO_CASES is rejected — auto-scoring always analyzes a real
      transcript.
    - ``notes`` stores the (truncated) transcript as an audit trail
      for the AI-produced deductions; ``qa_id`` is injected from the
      authenticated caller.
    """
    agent = await _get_support_agent_or_error(payload.agent_id, db)
    # Same quota rule as POST /api/reviews, checked BEFORE the paid AI
    # call: a saved auto-scored review counts towards the quota.
    await _reject_quota_reached(agent, db)

    # End the quota-check read transaction. The AI service re-reads the
    # system prompt and scorecard rules on the same session and commits
    # again after those reads, so the pooled connection is released for
    # the multi-second LLM call itself and re-acquired on the insert.
    await db.commit()

    # AI analysis. Nothing is saved when this fails: ValueError means
    # the API key is unset (503); AnalyzeError means the call or the
    # response parsing failed (502). The session was committed above,
    # so the rules query inside the service starts a fresh (read-only)
    # transaction; the connection may sit idle during the network call
    # — accepted trade-off, see analyze_support_ticket.
    try:
        raw_scorecard = await ai_service.analyze_support_ticket(
            payload.transcript, payload.case_type, db
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "in the environment or the .env file to enable AI auto-scoring."
            ),
        ) from exc
    except ai_service.AnalyzeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI analysis failed: {exc}",
        ) from exc

    breakdown, final_score, total_penalty = (
        await multiplier_service.calculate_final_score(agent.id, raw_scorecard, db)
    )
    rules_snapshot = await scorecard_service.get_active_rules(payload.case_type, db)
    scorecard_data = {
        "rules_snapshot": rules_snapshot,
        "base_score": multiplier_service.BASE_SCORE,
        "total_penalty": total_penalty,
        "final_score": final_score,
        "breakdown": breakdown,
    }

    review = Review(
        support_agent_id=agent.id,
        qa_id=current_user.id,
        case_type=payload.case_type,
        case_number=payload.case_number,
        scorecard_data=scorecard_data,
        # Keep the (truncated) transcript as the audit trail for the
        # AI-produced deductions.
        notes=f"[auto-scored] {payload.transcript[:2000]}",
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
    current_user: AuthUser,
    year: int | None = Query(default=None, ge=1900, le=3000),
    month: int | None = Query(default=None, ge=1, le=12),
) -> QuotaRead:
    """Quota status for the current reporting period (or the given
    CLOSING year/month).

    RBAC: reviewer roles unrestricted; Support-only users may query
    only their own agent_id (403 otherwise).
    """
    if not is_reviewer(current_user) and agent_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support agents may only query their own quota.",
        )

    agent = await db.get(User, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support agent {agent_id} not found.",
        )

    current_year, current_month = _current_closing_period()
    quota = await quota_service.get_agent_quota(
        agent_id,
        year if year is not None else current_year,
        month if month is not None else current_month,
        db,
    )
    return QuotaRead(**quota)


@router.get("/quota-compliance/{qa_id}", response_model=QuotaComplianceRead)
async def get_quota_compliance(
    qa_id: int,
    db: DbSession,
    current_user: ReviewerUser,
    year: int | None = Query(default=None, ge=1900, le=3000),
    month: int | None = Query(default=None, ge=1, le=12),
) -> QuotaComplianceRead:
    """Per-interval quota compliance for one QA over a reporting period.

    Assigned agents are the DISTINCT non-null support_agent_id values
    of this QA's assignments (General + Hybrid). Each interval
    requires one review per assigned agent; completed counts DISTINCT
    reviewed agents within the half-open interval range.

    RBAC: Admin/Supervisor may query any qa_id; a QA only their own
    qa_id (403 otherwise); Support-only always 403 (blocked by the
    ReviewerUser dependency).
    """
    privileged = current_user.has_role(RoleEnum.ADMIN, RoleEnum.SUPERVISOR)
    if not privileged:
        if not current_user.has_role(RoleEnum.QA) or qa_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A QA may only query their own quota compliance.",
            )

    qa_user = await db.get(User, qa_id)
    if qa_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"QA user {qa_id} not found.",
        )

    current_year, current_month = _current_closing_period()
    closing_year = year if year is not None else current_year
    closing_month = month if month is not None else current_month

    compliance = await quota_service.get_qa_compliance(
        qa_id, closing_year, closing_month, db
    )
    return QuotaComplianceRead(
        qa_id=qa_id,
        closing_year=closing_year,
        closing_month=closing_month,
        period_label=reporting_period.period_label(closing_year, closing_month),
        assigned_agent_count=compliance["assigned_agent_count"],
        intervals=[
            IntervalComplianceRead(**interval)
            for interval in compliance["intervals"]
        ],
        total_required=compliance["total_required"],
        total_completed=compliance["total_completed"],
        total_deficit=compliance["total_deficit"],
    )


@router.get("/{review_id}", response_model=ReviewRead)
async def get_review(
    review_id: int,
    db: DbSession,
    current_user: AuthUser,
) -> ReviewRead:
    """Fetch a single review by id (404 if it does not exist).

    RBAC: reviewer roles see everything; Support-only users may fetch
    only reviews where they are the reviewed agent — another agent's
    review yields an explicit 403 Forbidden (not 404).
    """
    review = await db.get(Review, review_id)
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review {review_id} not found.",
        )
    if not is_reviewer(current_user) and review.support_agent_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support agents may only view their own reviews.",
        )
    return ReviewRead.model_validate(review)
