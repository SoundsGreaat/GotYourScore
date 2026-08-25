"""Reviews API endpoints.

- POST /api/reviews: create a review (rule snapshot embedded into
  scorecard_data, progressive multiplier scoring, reporting-period
  quota enforcement, 'No Cases' edge case).
- POST /api/reviews/pending: Supervisor/Admin delegates a review of a
  real case to a named QA — or leaves it unassigned in the shared
  queue (status='pending', no scores, quota-neutral until completed;
  409 when an open handoff for the same agent + case_number exists).
- POST /api/reviews/pending/bulk: Supervisor/Admin delegates a LIST of
  fully-resolved pending reviews in one transaction; per-row failures
  come back as skips with reasons instead of aborting the batch. Rows
  may omit ``assigned_qa_id`` to land in the shared queue.
- GET /api/reviews/pending/mine-count: count of pending work AVAILABLE
  to the calling QA — assigned to them PLUS the unassigned shared
  queue (sidebar badge polling).
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
- PATCH /api/reviews/{review_id}: edit case_type/case_number/notes,
  optionally rescore with a new raw_scorecard, and — pending reviews
  only — reassign support_agent_id/assigned_qa_id (tri-state keys:
  absent = unchanged, present = apply, explicit null on
  assigned_qa_id = back to the shared queue). Providing raw_scorecard
  on a pending row completes it; EVERY successful pending edit makes
  the editor the executor (qa_id), while completed-review edits never
  touch qa_id.
- DELETE /api/reviews/{review_id}: soft delete (sets deleted_at; the
  row stops counting towards quotas and disappears from the API).

RBAC matrix:
- POST endpoints: QA/Supervisor/Admin only — Support-only users get
  403 from the RoleChecker; a Support+QA hybrid passes and may review
  other Support agents. EXCEPTION: POST /pending and POST /pending/bulk
  are Supervisor/Admin only (delegation is a staffing decision).
- GET /pending/mine-count: reviewer roles only (Support-only users get
  403 from the RoleChecker); always scoped to the calling user.
- PATCH /{review_id} and DELETE /{review_id}: any QA/Supervisor/Admin
  may edit or soft-delete ANY review — explicit product decision, no
  ownership check (vacation coverage: whoever edits a pending review
  last becomes its executor, and completing a delegated review makes
  the completer its reviewer of record).
- GET /quota/{agent_id}: reviewer roles unrestricted; Support-only
  users may query only their own agent_id (403 otherwise).
- GET /quota-compliance/{qa_id}: Admin/Supervisor may query any qa_id;
  a QA only their own qa_id (403 otherwise); Support-only always 403.
- GET /{review_id}: reviewer roles see everything; Support-only users
  may fetch only reviews where they are the reviewed agent — accessing
  another agent's review is an explicit 403 (never a 404). Soft-deleted
  reviews yield 404 for everyone.
"""

from datetime import datetime, timedelta, timezone
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker, get_current_user, is_reviewer
from app.db.database import get_db
from app.models import CaseTypeEnum, Review, ReviewStatusEnum, RoleEnum, User
from app.schemas.review import (
    AutoScoreCreate,
    IntervalComplianceRead,
    PendingBulkCreate,
    PendingBulkCreatedRow,
    PendingBulkResponse,
    PendingBulkSkippedRow,
    PendingCountRead,
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

# ReviewCreate.period wire format: the closing month of a reporting
# period ("YYYY-MM"). Format is already enforced by the schema pattern;
# this re-check powers the endpoint-level future-period rejection.
_PERIOD_RE = re.compile(r"^(\d{4})-(1[0-2]|0[1-9])$")

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


def _backfill_period(
    raw: str | None,
) -> tuple[int, int] | None:
    """Resolve ``ReviewCreate.period`` into a PAST closing (year, month).

    Returns None when the payload omits the period or targets the
    CURRENT one (both mean "stamp created_at with now"). Raises 400
    for a future period — backfilling work that has not happened yet
    would silently corrupt quota history.
    """
    if not raw:
        return None
    match = _PERIOD_RE.match(raw)
    if match is None:  # schema pattern already rejects; defense in depth
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid reporting period "
                f"{raw!r} — expected 'YYYY-MM'."
            ),
        )
    year, month = int(match.group(1)), int(match.group(2))
    current_year, current_month = _current_closing_period()
    if (year, month) > (current_year, current_month):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reporting period cannot be in the future.",
        )
    if (year, month) == (current_year, current_month):
        return None
    return year, month


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


async def _reject_quota_reached(
    agent: User,
    db: AsyncSession,
    closing: tuple[int, int] | None = None,
) -> None:
    """409 once the agent reached MONTHLY_QUOTA in the target period.

    ``closing`` selects an explicit backfill (past) period; None means
    the current one. Not race-free: two concurrent POSTs can both pass
    the check; use pg_advisory_xact_lock(agent_id) around check+insert
    if strictness is ever required.
    """
    closing_year, closing_month = closing or _current_closing_period()
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
    - ``period`` ("YYYY-MM", optional) BACKDATES the review into a
      past reporting period: the quota gate runs against that period
      and ``created_at`` is stamped to its last second (23:59:59 UTC
      on the closing month's 25th) so quota math and every listing
      agree. Omitted — or the current period — keeps the default
      "now"; a future period is a 400.
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
    backfill = _backfill_period(payload.period)
    await _reject_quota_reached(agent, db, backfill)

    if payload.case_type is CaseTypeEnum.NO_CASES:
        scorecard_data: dict[str, Any] | None = None
        final_score: int | None = None
    else:
        breakdown, final_score, total_penalty = (
            await multiplier_service.calculate_final_score(
                agent.id, payload.raw_scorecard or {}, db,
                no_multiplier_keys=set(payload.no_multiplier_keys),
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
            "multiplier_exemptions": sorted(payload.no_multiplier_keys),
        }

    # Backfill: pin the row to the chosen period's last second so it
    # lands inside that period's bounds everywhere (quota, partials).
    # None (or the current period) leaves created_at to server_default.
    created_at = None
    if backfill is not None:
        _, period_end = reporting_period.reporting_period_bounds(*backfill)
        created_at = period_end - timedelta(seconds=1)

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
        created_at=created_at,
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
            agent.id, payload.raw_scorecard, db,
            exclude_review_id=payload.exclude_review_id,
            no_multiplier_keys=set(payload.no_multiplier_keys),
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
    """Delegate a review of a real case to a named QA — or leave it
    unassigned in the shared queue (Supervisor/Admin only).

    Creates a ``status='pending'`` row: a handoff record with NO scores
    (``scorecard_data=None``, ``final_score=None``, ``notes=None``).
    The assignee (or any QA, for shared-queue rows) completes it later
    via PATCH with a raw_scorecard.

    Business rules:
    - Pending reviews do NOT touch the support agent's quota — they
      only start counting once completed (quota checks deliberately
      skipped here; see ``quota_service.counted_review_filters``).
    - ``case_type='No Cases'`` is rejected (400): a delegated review
      targets one specific real case.
    - ``case_number`` is optional; the schema strips whitespace and
      normalizes blanks to null. Rows WITH a number get duplicate
      protection (parity with the bulk endpoint): 409 when a
      non-deleted status='pending' review already exists for the same
      support agent + case number — numberless rows never collide.
    - ``assigned_qa_id`` is optional: null/omitted leaves the row
      UNASSIGNED in the shared queue any QA can grab; when provided it
      must reference an existing user holding the 'QA' role (400
      otherwise).

    Audit columns: ``qa_id`` starts as the delegating user's id but is
    OVERWRITTEN by whoever edits the row next (last editor becomes
    executor — see PATCH); ``created_by`` stays the delegator forever;
    ``assigned_qa_id`` records who was asked at delegation time (a
    pending-row PATCH may re-route it; it survives completion as the
    audit trail).
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

    # Assignee validation only binds when one was named — null routes
    # the handoff to the shared queue.
    if payload.assigned_qa_id is not None:
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

    # Duplicate protection parity with the bulk endpoint: one OPEN
    # handoff per (support agent, case number); rows without a number
    # never collide (NULL never matches in the tuple comparison).
    if payload.case_number is not None:
        result = await db.execute(
            select(Review.support_agent_id, Review.case_number).where(
                tuple_(Review.support_agent_id, Review.case_number).in_(
                    [(agent.id, payload.case_number)]
                ),
                Review.status == ReviewStatusEnum.PENDING,
                Review.deleted_at.is_(None),
            )
        )
        if result.first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Support agent {agent.id} already has a pending review "
                    f"for case_number '{payload.case_number}'."
                ),
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


@router.post("/pending/bulk", response_model=PendingBulkResponse)
async def create_pending_reviews_bulk(
    payload: PendingBulkCreate,
    db: DbSession,
    current_user: SupervisorAdminUser,
) -> PendingBulkResponse:
    """Bulk-delegate pending reviews (Supervisor/Admin only).

    Each row is fully resolved client-side and may target a DIFFERENT
    support agent / case type — supervisors paste whole spreadsheet
    selections. ``assigned_qa_id`` is optional per row: rows without
    one stay UNASSIGNED in the shared queue. ``case_number`` is
    optional per row too (whitespace normalizes to null). Rows are
    validated per row mirroring the single delegation endpoint, but
    one bad row never aborts the batch: it is reported in ``skipped``
    with a reason while the good rows are still created.

    Skip reasons (skips are not errors):
    - ``duplicate in list``: a later row repeats a (support_agent_id,
      case_number) pair already seen earlier in THIS payload (rows
      without a case_number are never duplicates — numberless rows
      cannot collide).
    - ``unknown or inactive agent``: support_agent_id does not resolve
      to an existing user holding the 'Support' role.
    - ``case_type 'No Cases' is not allowed``: parity with the single
      endpoint — a delegated review targets one specific real case.
    - ``assignee is not a QA``: assigned_qa_id does not reference an
      existing user holding the 'QA' role (only checked when provided —
      omitted assignees are valid and stay unassigned).
    - ``already pending``: a NON-deleted review with the same
      support_agent_id + case_number is already status='pending'
      (numberless rows never collide; completed or old rows do NOT
      block a re-delegation).

    Valid rows are created exactly like POST /pending (status='pending',
    ``qa_id``/``created_by`` = the delegator, null scorecard/final
    score, quota-neutral) in ONE transaction — a single commit at the
    end, so the batch lands atomically.

    The response rows are deliberately light (id + case_number, reason +
    case_number): a bulk response can cover hundreds of rows and callers
    only need enough to reconcile their spreadsheet; full rows remain
    fetchable via GET /api/reviews/{review_id}.

    Route order note: declared BEFORE any ``/{review_id}`` route so the
    literal path can never be captured as an id segment.
    """
    # Pass 1 — payload-level duplicates by identical (support_agent_id,
    # case_number) pairs: only LATER occurrences are skipped, the first
    # occurrence competes normally, and numberless rows never collide.
    seen_pairs: set[tuple[int, str]] = set()
    candidates: list[tuple[int, PendingBulkRow]] = []
    skip_reasons: dict[int, str] = {}
    for index, row in enumerate(payload.rows):
        if row.case_number is not None:
            pair = (row.support_agent_id, row.case_number)
            if pair in seen_pairs:
                skip_reasons[index] = "duplicate in list"
                continue
            seen_pairs.add(pair)
        candidates.append((index, row))

    # Batch-load every referenced user once (roles load eagerly via
    # selectin) instead of two point lookups per row. Shared-queue rows
    # carry no assignee — skip the None ids.
    user_ids = {
        rid
        for rid in {row.support_agent_id for _, row in candidates}
        | {row.assigned_qa_id for _, row in candidates}
        if rid is not None
    }
    users_by_id: dict[int, User] = {}
    if user_ids:
        result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_by_id = {user.id: user for user in result.scalars().all()}

    # Pass 2 — per-row business validation; failures collect instead of
    # aborting. Check order mirrors the single delegation endpoint:
    # agent first, then NO_CASES, then the assignee's QA role (only
    # when an assignee was named — null routes to the shared queue).
    valid_rows: list[tuple[int, PendingBulkRow]] = []
    for index, row in candidates:
        agent = users_by_id.get(row.support_agent_id)
        if agent is None or not agent.has_role(RoleEnum.SUPPORT):
            skip_reasons[index] = "unknown or inactive agent"
            continue
        if row.case_type is CaseTypeEnum.NO_CASES:
            skip_reasons[index] = "case_type 'No Cases' is not allowed"
            continue
        if row.assigned_qa_id is not None:
            assigned_qa = users_by_id.get(row.assigned_qa_id)
            if assigned_qa is None or not assigned_qa.has_role(RoleEnum.QA):
                skip_reasons[index] = "assignee is not a QA"
                continue
        valid_rows.append((index, row))

    # One query for every candidate pair that is ALREADY an open,
    # non-deleted handoff; completed/old rows do not block.
    existing_pairs: set[tuple[int, str]] = set()
    numbered_rows = [
        (index, row) for index, row in valid_rows if row.case_number is not None
    ]
    if numbered_rows:
        pairs = [
            (row.support_agent_id, row.case_number) for _, row in numbered_rows
        ]
        result = await db.execute(
            select(Review.support_agent_id, Review.case_number).where(
                tuple_(Review.support_agent_id, Review.case_number).in_(pairs),
                Review.status == ReviewStatusEnum.PENDING,
                Review.deleted_at.is_(None),
            )
        )
        existing_pairs = set(result.all())

    created_reviews: list[Review] = []
    for index, row in valid_rows:
        if (
            row.case_number is not None
            and (row.support_agent_id, row.case_number) in existing_pairs
        ):
            skip_reasons[index] = "already pending"
            continue
        review = Review(
            support_agent_id=row.support_agent_id,
            # Overwritten by the completing QA; starts as the delegator
            # so the row always has a valid reviewer reference.
            qa_id=current_user.id,
            created_by=current_user.id,
            status=ReviewStatusEnum.PENDING,
            assigned_qa_id=row.assigned_qa_id,
            case_type=row.case_type,
            case_number=row.case_number,
            scorecard_data=None,
            notes=None,
            final_score=None,
        )
        db.add(review)
        created_reviews.append(review)

    # Flush (not commit) so ids are assigned for the response while all
    # rows still land atomically in the single commit below.
    if created_reviews:
        await db.flush()

    response = PendingBulkResponse(
        created=[
            PendingBulkCreatedRow(id=review.id, case_number=review.case_number)
            for review in created_reviews
        ],
        skipped=[
            PendingBulkSkippedRow(
                case_number=payload.rows[index].case_number, reason=reason
            )
            for index, reason in sorted(skip_reasons.items())
        ],
        created_count=len(created_reviews),
        skipped_count=len(skip_reasons),
    )
    await db.commit()
    return response


@router.get("/pending/mine-count", response_model=PendingCountRead)
async def count_my_pending_reviews(
    db: DbSession,
    current_user: ReviewerUser,
) -> PendingCountRead:
    """Count pending work AVAILABLE to the calling QA.

    COUNT of non-deleted status='pending' reviews that are either
    ASSIGNED to the caller (``assigned_qa_id`` = caller) or UNASSIGNED
    in the shared queue (``assigned_qa_id`` IS NULL) — i.e. everything
    the caller could pick up in the To-review view. Powers the
    dashboard sidebar badge polling; Support-only users are 403 via
    ReviewerUser.
    """
    count = (
        await db.execute(
            select(func.count())
            .select_from(Review)
            .where(
                or_(
                    Review.assigned_qa_id == current_user.id,
                    Review.assigned_qa_id.is_(None),
                ),
                Review.status == ReviewStatusEnum.PENDING,
                Review.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return PendingCountRead(count=int(count))


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

    Assigned agents are the DISTINCT support_agent_id values of this
    QA's assignments. Credit is SCOPE-BASED,
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
    - Tri-state keys resolved via ``model_fields_set``: a key ABSENT
      from the JSON leaves its column untouched; a key PRESENT is
      applied. ``support_agent_id``/``assigned_qa_id`` are honored
      ONLY for pending reviews (400 "Only pending reviews can be
      reassigned." otherwise): ``support_agent_id`` must resolve to an
      existing Support user (404/400 like creation); ``assigned_qa_id``
      must resolve to an existing QA user, with an explicit ``null``
      clearing the assignment back to the shared queue.
    - Pending edits WITHOUT raw_scorecard are legal metadata edits:
      the row keeps its null scorecard_data/final_score and stays
      ``pending`` (the stored-scorecard requirement below only binds
      completed rows).
    - Completion = providing raw_scorecard on a pending row (an
      explicitly EMPTY dict counts — a clean case scores the base
      100). Full recompute: progressive multipliers over the agent's
      past scorecards plus a FRESH rules snapshot from the currently
      active rules (same shape as create_review).
    - case_type 'No Cases' is rejected for ALL pending edits (400) —
      a delegated handoff targets a real case, metadata tweak or not.
      On completed rows the legacy rules apply: a resulting 'No Cases'
      nulls scorecard_data/final_score and rejects any provided
      raw_scorecard; a real-case result without raw_scorecard and no
      stored scorecard_data is a 400 (nothing to keep).
    - case_number: pending rows apply the key whenever PRESENT and
      normalize — whitespace-only (or explicit null) stores null, so
      blanking/un-blanking a handoff is legal now that creation allows
      numberless rows. Completed rows keep the legacy behavior: only
      non-null values apply, stored verbatim.
    - LAST EDITOR BECOMES EXECUTOR: every successful pending PATCH —
      metadata edit or completion — sets ``qa_id`` to the editing
      user; only an edit WITH a raw_scorecard additionally flips the
      row to ``completed``. Editing an already-completed review never
      flips ``status`` and never changes ``qa_id``. Completion leaves
      ``assigned_qa_id`` and ``created_by`` untouched (audit trail of
      who-was-asked/who-delegated).
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

    is_pending = review.status == ReviewStatusEnum.PENDING
    # Completion is triggered BY the scorecard: providing raw_scorecard
    # on a pending row means "score it and finish it".
    completing = is_pending and payload.raw_scorecard is not None
    effective_case_type = (
        payload.case_type if payload.case_type is not None else review.case_type
    )
    provided = payload.model_fields_set

    # Reassignment keys bind to pending rows only.
    if (
        "support_agent_id" in provided or "assigned_qa_id" in provided
    ) and not is_pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending reviews can be reassigned.",
        )

    # Resolve reassignment targets up front, before any mutation.
    new_support_agent: User | None = None
    new_assigned_qa: User | None = None
    if is_pending and "support_agent_id" in provided:
        if payload.support_agent_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "support_agent_id cannot be cleared — every review "
                    "targets a Support agent."
                ),
            )
        new_support_agent = await _get_support_agent_or_error(
            payload.support_agent_id, db
        )
    if (
        is_pending
        and "assigned_qa_id" in provided
        and payload.assigned_qa_id is not None
    ):
        new_assigned_qa = await db.get(User, payload.assigned_qa_id)
        if new_assigned_qa is None or not new_assigned_qa.has_role(RoleEnum.QA):
            roles = (
                ", ".join(role.value for role in new_assigned_qa.roles)
                if new_assigned_qa is not None
                else None
            )
            detail = (
                f"User {payload.assigned_qa_id} has roles '{roles}' but pending "
                "reviews must be delegated to a user with role 'QA'."
                if new_assigned_qa is not None
                else f"Assigned QA {payload.assigned_qa_id} not found."
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=detail
            )

    if is_pending:
        # ALL pending edits reject 'No Cases' — a delegated handoff
        # targets a real case, metadata tweak or completion alike.
        if effective_case_type is CaseTypeEnum.NO_CASES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "A delegated pending review must stay a scored real "
                    "case — case_type 'No Cases' is not allowed on "
                    "pending reviews."
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
    elif (
        not payload.raw_scorecard
        and review.scorecard_data is None
        and not is_pending
    ):
        # Completed rows only: a pending row legitimately has no stored
        # scorecard yet (kept null until completion).
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
    if is_pending:
        # Key-presence semantics: explicit null (or whitespace-only)
        # un-blanks the handoff — legal since creation made the number
        # optional. Non-blank values are stored stripped.
        if "case_number" in provided:
            stripped = (
                payload.case_number.strip()
                if payload.case_number is not None
                else ""
            )
            review.case_number = stripped or None
    elif payload.case_number is not None:
        # Legacy completed-row behavior: verbatim, only when non-null.
        review.case_number = payload.case_number
    if payload.notes is not None:
        review.notes = payload.notes
    if new_support_agent is not None:
        review.support_agent_id = new_support_agent.id
    if "assigned_qa_id" in provided:
        review.assigned_qa_id = (
            new_assigned_qa.id if new_assigned_qa is not None else None
        )

    if effective_case_type is CaseTypeEnum.NO_CASES:
        review.scorecard_data = None
        review.final_score = None
    elif payload.raw_scorecard is not None:
        # Absent no_multiplier_keys keeps the stored exemptions so a
        # rescore reproduces the original waives; present replaces them.
        if "no_multiplier_keys" in provided:
            multiplier_exemptions = sorted(payload.no_multiplier_keys or [])
        else:
            multiplier_exemptions = list(
                (review.scorecard_data or {}).get("multiplier_exemptions")
                or []
            )
        breakdown, final_score, total_penalty = (
            await multiplier_service.calculate_final_score(
                review.support_agent_id, payload.raw_scorecard, db,
                exclude_review_id=review.id,
                no_multiplier_keys=set(multiplier_exemptions),
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
            "multiplier_exemptions": multiplier_exemptions,
        }
        review.final_score = final_score

    # LAST EDITOR BECOMES EXECUTOR: every successful pending PATCH —
    # metadata edit or completion — reassigns qa_id to the editor;
    # only an edit WITH a raw_scorecard additionally flips the row to
    # completed. Edits of already-completed rows never touch status
    # or qa_id.
    if is_pending:
        review.qa_id = current_user.id
    if completing:
        review.status = ReviewStatusEnum.COMPLETED

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
