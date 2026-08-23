"""Reviews API endpoints.

- POST /api/reviews: create a review (rule snapshot embedded into
  scorecard_data, progressive multiplier scoring, reporting-period
  quota enforcement, 'No Cases' edge case).
- POST /api/reviews/pending: Supervisor/Admin delegates a review of a
  real case to a named QA (status='pending', no scores, quota-neutral
  until completed).
- POST /api/reviews/auto-score: create a review from an AI-analyzed
  ticket transcript (OpenRouter; 503 when unconfigured, 502 when the
  analysis fails).
- POST /api/reviews/score-preview: read-only preview of the breakdown
  a save would produce for a raw scorecard (nothing persisted).
- GET /api/reviews/quota/{agent_id}: quota status for the current (or
  explicitly requested) REPORTING period.
- GET /api/reviews/quota-compliance/{qa_id}: per-interval quota
  compliance for one QA over a reporting period.
- GET /api/reviews/{review_id}: fetch a single review (404 once
  soft-deleted).
- PATCH /api/reviews/{review_id}: edit case_type/case_number/notes and
  optionally rescore with a new raw_scorecard; completing a pending
  review REQUIRES a raw_scorecard and flips it to completed with the
  completer becoming the reviewer of record.
- DELETE /api/reviews/{review_id}: soft delete (sets deleted_at; the
  row stops counting towards quotas and disappears from the API).

RBAC matrix:
- POST endpoints: QA/Supervisor/Admin only — Support-only users get
  403 from the RoleChecker; a Support+QA hybrid passes and may review
  other Support agents. EXCEPTION: POST /pending is Supervisor/Admin
  only (delegation is a staffing decision).
- PATCH /{review_id} and DELETE /{review_id}: any QA/Supervisor/Admin
  may edit or soft-delete ANY review — explicit product decision, no
  ownership check (vacation coverage: whoever completes a delegated
  review becomes its reviewer of record).
- GET /quota/{agent_id}: reviewer roles unrestricted; Support-only
  users may query only their own agent_id (403 otherwise).
- GET /quota-compliance/{qa_id}: Admin/Supervisor may query any qa_id;
  a QA only their own qa_id (403 otherwise); Support-only always 403.
- GET /{review_id}: reviewer roles see everything; Support-only users
  may fetch only reviews where they are the reviewed agent — accessing
  another agent's review is an explicit 403 (never a 404). Soft-deleted
  reviews yield 404 for everyone.
"""

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker, get_current_user, is_reviewer
from app.db.database import get_db
from app.models import CaseTypeEnum, Review, ReviewStatusEnum, RoleEnum, User
from app.schemas.review import (
    AutoScoreCreate,
    IntervalComplianceRead,
    PendingReviewCreate,
    QuotaComplianceRead,
    QuotaRead,
    ReviewCreate,
    ReviewRead,
    ReviewUpdate,
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

# Delegation (pending reviews) and other staffing-shaped actions are
# restricted to supervisors and admins.
SupervisorAdminUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
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
    - ``qa_id`` is injected from the authenticated caller, who is also
      recorded as ``created_by``; the row is created ``completed``.
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
        created_by=current_user.id,
        status=ReviewStatusEnum.COMPLETED,
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
        created_by=current_user.id,
        status=ReviewStatusEnum.COMPLETED,
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


@router.post(
    "/pending", response_model=ReviewRead, status_code=status.HTTP_201_CREATED
)
async def create_pending_review(
    payload: PendingReviewCreate,
    db: DbSession,
    current_user: SupervisorAdminUser,
) -> ReviewRead:
    """Delegate a review of a real case to a named QA
    (Supervisor/Admin only).

    Creates a ``status='pending'`` row: a handoff record with NO scores
    (``scorecard_data=None``, ``final_score=None``, ``notes=None``).
    The named QA completes it later via PATCH with a raw_scorecard.

    Business rules:
    - Pending reviews do NOT touch the support agent's quota — they
      only start counting once completed (quota checks deliberately
      skipped here; see ``quota_service.counted_review_filters``).
    - ``case_type='No Cases'`` is rejected (400): a delegated review
      targets one specific real case.
    - ``case_number`` is required and must not be blank/whitespace
      (400): "which case?" must be answerable from the row alone.
    - ``assigned_qa_id`` must reference an existing user holding the
      'QA' role (400 otherwise).

    Audit columns: ``qa_id`` starts as the delegating user's id but is
    OVERWRITTEN by the completing QA on completion; ``created_by``
    stays the delegator forever; ``assigned_qa_id`` records who was
    asked and is never rewritten.
    """
    agent = await _get_support_agent_or_error(payload.support_agent_id, db)

    if payload.case_type is CaseTypeEnum.NO_CASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "A delegated pending review targets a real case — "
                "case_type 'No Cases' is not allowed."
            ),
        )
    if not payload.case_number.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="case_number is required for a delegated pending review.",
        )

    assigned_qa = await db.get(User, payload.assigned_qa_id)
    if assigned_qa is None or not assigned_qa.has_role(RoleEnum.QA):
        roles = (
            ", ".join(role.value for role in assigned_qa.roles)
            if assigned_qa is not None
            else None
        )
        detail = (
            f"User {payload.assigned_qa_id} has roles '{roles}' but pending "
            "reviews must be delegated to a user with role 'QA'."
            if assigned_qa is not None
            else f"Assigned QA {payload.assigned_qa_id} not found."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=detail
        )

    review = Review(
        support_agent_id=agent.id,
        # Overwritten by the completing QA; starts as the delegator so
        # the row always has a valid reviewer reference.
        qa_id=current_user.id,
        created_by=current_user.id,
        status=ReviewStatusEnum.PENDING,
        assigned_qa_id=payload.assigned_qa_id,
        case_type=payload.case_type,
        case_number=payload.case_number,
        scorecard_data=None,
        notes=None,
        final_score=None,
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
    of this QA's assignments (General + Hybrid). Credit is SCOPE-BASED,
    not performer-based: completed counts are plain COUNTs of counted
    reviews of those agents within each half-open interval range and
    over the whole period — no ``qa_id`` filter, no DISTINCT collapse.

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
    """Fetch a single review by id (404 if it does not exist or was
    soft-deleted).

    RBAC: reviewer roles see everything; Support-only users may fetch
    only reviews where they are the reviewed agent — another agent's
    review yields an explicit 403 Forbidden (not 404). Soft-deleted
    rows yield 404 for everyone.
    """
    review = await db.get(Review, review_id)
    if review is None or review.deleted_at is not None:
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


@router.patch("/{review_id}", response_model=ReviewRead)
async def update_review(
    review_id: int,
    payload: ReviewUpdate,
    db: DbSession,
    current_user: ReviewerUser,
) -> ReviewRead:
    """Edit a review (any QA/Supervisor/Admin may edit ANY review —
    explicit product decision, no ownership check, so vacation cover
    works).

    Semantics:
    - 404 for unknown ids AND soft-deleted rows (deleted reviews are
      indistinguishable from missing ones).
    - Only provided fields change; ``support_agent_id`` is immutable
      (not part of the payload).
    - Resulting case type 'No Cases': ``scorecard_data`` and
      ``final_score`` are nulled and any provided raw_scorecard is
      rejected (400).
    - Resulting case type is a real case AND raw_scorecard provided:
      full recompute — progressive multipliers over the agent's past
      scorecards plus a FRESH rules snapshot from the currently active
      rules (same shape as create_review).
    - Real case, no raw_scorecard, but the row has no stored
      scorecard_data yet (e.g. case_type flipped away from 'No Cases',
      or a pending handoff): 400 "raw_scorecard required" — there is
      nothing to keep.
    - Completing a pending review: raw_scorecard is REQUIRED (400
      otherwise — a delegated real case must actually be scored) and
      'No Cases' completion is rejected (400). On success the row flips
      to ``completed`` and the COMPLETER becomes ``qa_id`` (reviewer of
      record); ``assigned_qa_id`` and ``created_by`` stay untouched as
      the audit trail of who-was-asked/who-delegated.
    - Editing an already-completed review never flips ``status`` and
      never changes ``qa_id``. Changing ONLY case_type without a new
      raw_scorecard keeps the previously stored scorecard_data (and its
      historical rules snapshot) as-is.
    - Quota: completion deliberately skips the quota-reached gate — a
      delegated case the QA actually reviewed is never blocked, so the
      agent can end the period above target (e.g. 7/6). Pending rows
      were quota-neutral, so the count only grows at completion.
    """
    review = await db.get(Review, review_id)
    if review is None or review.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review {review_id} not found.",
        )

    completing = review.status == ReviewStatusEnum.PENDING
    effective_case_type = (
        payload.case_type if payload.case_type is not None else review.case_type
    )

    if completing:
        if effective_case_type is CaseTypeEnum.NO_CASES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "A delegated pending review must be completed as a "
                    "scored real case — 'No Cases' completion is not allowed."
                ),
            )
        if payload.raw_scorecard is None:
            # An explicitly EMPTY dict is valid (a clean case scores the
            # base 100, same as creation); only a MISSING key is rejected.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "raw_scorecard is required to complete a pending "
                    "review — the delegated case must be scored."
                ),
            )
    elif effective_case_type is CaseTypeEnum.NO_CASES:
        if payload.raw_scorecard:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Reviews with case_type='No Cases' must have a null or "
                    "empty raw_scorecard."
                ),
            )
    elif not payload.raw_scorecard and review.scorecard_data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "raw_scorecard required: this review has no stored "
                "scorecard data to keep."
            ),
        )

    # Only provided fields change.
    if payload.case_type is not None:
        review.case_type = payload.case_type
    if payload.case_number is not None:
        if not payload.case_number.strip() and (
            completing or review.status is ReviewStatusEnum.PENDING
        ):
            # A delegated handoff must keep pointing at a real case.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "case_number cannot be blanked on a pending review — "
                    "delete the handoff instead."
                ),
            )
        review.case_number = payload.case_number
    if payload.notes is not None:
        review.notes = payload.notes

    if effective_case_type is CaseTypeEnum.NO_CASES:
        review.scorecard_data = None
        review.final_score = None
    elif payload.raw_scorecard:
        breakdown, final_score, total_penalty = (
            await multiplier_service.calculate_final_score(
                review.support_agent_id, payload.raw_scorecard, db
            )
        )
        rules_snapshot = await scorecard_service.get_active_rules(
            effective_case_type, db
        )
        # Rebuild the whole dict (never mutate in place): JSONB change
        # tracking relies on attribute reassignment.
        review.scorecard_data = {
            "rules_snapshot": rules_snapshot,
            "base_score": multiplier_service.BASE_SCORE,
            "total_penalty": total_penalty,
            "final_score": final_score,
            "breakdown": breakdown,
        }
        review.final_score = final_score

    if completing:
        review.status = ReviewStatusEnum.COMPLETED
        # The completer becomes the reviewer of record; assigned_qa_id
        # and created_by remain untouched (audit).
        review.qa_id = current_user.id

    await db.commit()
    await db.refresh(review)
    return ReviewRead.model_validate(review)


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: int,
    db: DbSession,
    _current_user: ReviewerUser,
) -> None:
    """Soft-delete a review (any QA/Supervisor/Admin may delete ANY
    review, including pending ones — deleting a pending handoff cancels
    it).

    Only ``deleted_at`` is set; every other column stays intact so an
    operator can restore the row at the SQL level. Soft-deleted rows
    stop counting towards quotas immediately
    (``quota_service.counted_review_filters``), disappear from GET
    endpoints and 404 on repeat deletes.
    """
    review = await db.get(Review, review_id)
    if review is None or review.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review {review_id} not found.",
        )
    review.deleted_at = _utcnow()
    await db.commit()
