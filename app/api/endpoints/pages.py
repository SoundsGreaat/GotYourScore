"""Server-rendered HTML pages (Jinja2).

- GET /login: public login page; visitors holding a valid session
  are redirected straight to the dashboard.
- GET /: dashboard; unauthenticated visitors are redirected to
  /login instead of receiving a 401 JSON error.
- GET /partials/my-reviews, GET /partials/all-reviews,
  GET /partials/team-quotas,
  GET /partials/qa-matrix and GET /partials/review-drawer: small HTML
  fragments swapped into the dashboard by HTMX; protected like the
  dashboard (303 redirect to /login when unauthenticated).

RBAC: the global aggregate partials (qa-matrix, team-quotas,
all-reviews) are reviewer-only — Support-only users receive a bare 403
response (HTMX surfaces it; no redirect). my-reviews is personal and
review-drawer is left accessible for all authenticated users.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import (
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select

from app.core.security import get_current_user, is_reviewer
from app.db.database import AsyncSession, get_db
from app.models import (
    CaseTypeEnum,
    QAAssignment,
    Review,
    ReviewStatusEnum,
    RoleEnum,
    User,
    UserRole,
)
from app.services import quota_service, reporting_period, scorecard_service

router = APIRouter(tags=["pages"])

# English month abbreviations, hardcoded so period labels never depend
# on the process locale (Windows strftime %b follows the locale).
_MONTH_ABBREVS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _period_range_label(start: datetime, end_exclusive: datetime) -> str:
    """Locale-safe English range label for a period's bounds.

    The exclusive end minus one day is the last covered day, so the
    Jul 26 – Aug 25 period renders as ``"Jul 26 – Aug 25"``.
    """
    last_day = end_exclusive - timedelta(days=1)
    return (
        f"{_MONTH_ABBREVS[start.month - 1]} {start.day}"
        f" \u2013 {_MONTH_ABBREVS[last_day.month - 1]} {last_day.day}"
    )

# Anchored to this file so the app works regardless of the CWD it is
# launched from (service manager, container, --app-dir, ...).
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


async def _session_user(request: Request, db: AsyncSession) -> User | None:
    """Return the session's user, or None when missing/invalid.

    Reuses ``get_current_user`` so "valid session" means exactly what
    the API endpoints enforce: a user_id in the session whose user
    still exists in the database.
    """
    try:
        return await get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


async def get_current_user_or_redirect(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | RedirectResponse:
    """Page-friendly wrapper around ``get_current_user``.

    Converts the 401 raised for missing/invalid sessions into a 303
    redirect to /login, so HTML pages bounce unauthenticated visitors
    to the login page instead of surfacing a JSON error.
    """
    user = await _session_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return user


# Dependency alias: the authenticated User, or a RedirectResponse to
# /login when the session is missing or invalid.
PageUser = Annotated[User | RedirectResponse, Depends(get_current_user_or_redirect)]


async def _nickname_map(
    db: AsyncSession, person_ids: set[int | None]
) -> dict[int, str]:
    """Batch-resolve user ids to display names.

    ``User.nickname`` is a computed Python property derived from the
    email local part — NOT a column — so names are materialized here
    with one ``in_()`` query instead of a SQL-level join.
    """
    ids = sorted({rid for rid in person_ids if rid is not None})
    if not ids:
        return {}
    people = (
        await db.execute(select(User).where(User.id.in_(ids)))
    ).scalars().all()
    return {person.id: person.nickname for person in people}


@router.get("/login", name="login", summary="Login page")
async def login_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the public login page.

    A visitor with a valid session goes straight to the dashboard;
    missing or stale sessions simply get the login page (never a 401).
    """
    if await _session_user(request, db) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": request.query_params.get("error")},
    )


@router.get("/", name="dashboard", summary="Dashboard page")
async def dashboard(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the dashboard for authenticated users.

    The template hides reviewer chrome (New Review button, tracker
    tabs, aggregate sidebar entries) from Support-only users; the
    server-side RBAC on the partials stays the source of truth.
    ``is_reviewer`` + ``mine_pending_count`` feed the reviewer-only
    "To review" sidebar link and its pending-count badge (available
    work: assigned to the caller or in the shared queue; the badge
    then keeps itself fresh via client-side polling).
    """
    if isinstance(auth, RedirectResponse):
        return auth
    reviewer = is_reviewer(auth)
    mine_pending_count = 0
    if reviewer:
        mine_pending_count = (
            await db.execute(
                select(func.count())
                .select_from(Review)
                .where(
                    or_(
                        Review.assigned_qa_id == auth.id,
                        Review.assigned_qa_id.is_(None),
                    ),
                    Review.status == ReviewStatusEnum.PENDING,
                    Review.deleted_at.is_(None),
                )
            )
        ).scalar_one()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": auth,
            "is_support_only": auth.is_support_only,
            "can_review": auth.has_role(
                RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN
            ),
            "is_admin": auth.has_role(RoleEnum.ADMIN),
            "is_reviewer": reviewer,
            "mine_pending_count": int(mine_pending_count),
        },
    )


def _htmx_redirect_if_needed(auth: object, request: Request) -> Response | None:
    """Adapt an auth RedirectResponse for HTMX requests.

    A plain 303 would make HTMX follow the redirect and swap the whole
    login page into #main-content; the HX-Redirect header instead makes
    the browser navigate to /login properly. Returns None when the
    request is authenticated and rendering should proceed.
    """
    if not isinstance(auth, RedirectResponse):
        return None
    if request.headers.get("HX-Request"):
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": "/login"})
    return auth


@router.get(
    "/partials/my-reviews",
    name="partial_my_reviews",
    summary="My Reviews partial",
    include_in_schema=False,
)
async def partial_my_reviews(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the personal reviews view (see :func:`_render_reviews_view`)."""
    return await _render_reviews_view(request, auth, db, scope="my")


@router.get(
    "/partials/all-reviews",
    name="partial_all_reviews",
    summary="All Reviews partial",
    include_in_schema=False,
)
async def partial_all_reviews(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the team-wide reviews view (see :func:`_render_reviews_view`)."""
    return await _render_reviews_view(request, auth, db, scope="all")


async def _render_reviews_view(
    request: Request,
    auth: PageUser,
    db: AsyncSession,
    *,
    scope: str,
) -> Response:
    """Render the shared reviews table used by BOTH sidebar submenus.

    Scope "my" is the personal view: reviewer roles (QA/Supervisor/Admin)
    see the reviews THEY performed — plus cases still PENDING that were
    delegated to them — while Support-only users see the reviews
    performed ABOUT them. Scope "all" is reviewer-only and shows every
    review of everyone; Support-only users get a bare 403 like the other
    aggregate partials.

    Both scopes are restricted to the CURRENT reporting period (26th ->
    25th UTC, named after its closing month). Soft-deleted rows are
    excluded everywhere. Optional filters arrive as query params:
    ``agent`` / ``qa`` (ids), ``case_type`` (enum value) and
    ``case_number`` (case-insensitive substring); invalid values are
    silently ignored so a stale bookmark never 500s. Person filters only
    apply for reviewers — a personal agent view has no other people to
    filter on. Counterpart display names are batch-loaded with a single
    in_() query (no N+1). Per-row action flags: ``can_complete`` marks
    pending rows assigned to the viewer; ``can_edit`` is true for every
    reviewer-visible row — the reviews API deliberately lets ANY
    QA/Supervisor/Admin edit or delete ANY review, so the all-scope
    table exposes those buttons on foreign rows too.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    if scope == "all" and not is_reviewer(auth):
        return _forbidden()

    performed_by_me = auth.has_role(
        RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN
    )
    period_start, period_end, closing_year, closing_month = (
        reporting_period.reporting_period_for(datetime.now(timezone.utc))
    )

    if scope == "all":
        stmt = select(Review).where(Review.deleted_at.is_(None))
    elif performed_by_me:
        stmt = select(Review).where(
            or_(
                Review.qa_id == auth.id,
                and_(
                    Review.status == ReviewStatusEnum.PENDING,
                    Review.assigned_qa_id == auth.id,
                ),
            ),
            Review.deleted_at.is_(None),
        )
    else:
        stmt = select(Review).where(
            Review.support_agent_id == auth.id,
            Review.deleted_at.is_(None),
        )
    # Current reporting period only.
    stmt = stmt.where(
        Review.created_at >= period_start,
        Review.created_at < period_end,
    )

    params = request.query_params

    def _int_param(name: str) -> int | None:
        try:
            return int(params.get(name) or "")
        except ValueError:
            return None

    f_agent = _int_param("agent") if performed_by_me else None
    f_qa = _int_param("qa") if performed_by_me else None
    f_case_type = params.get("case_type") or ""
    if f_case_type:
        try:
            CaseTypeEnum(f_case_type)
        except ValueError:
            f_case_type = ""
    f_case_number = (params.get("case_number") or "").strip()

    if performed_by_me:
        if f_agent:
            stmt = stmt.where(Review.support_agent_id == f_agent)
        if f_qa:
            stmt = stmt.where(
                or_(
                    Review.qa_id == f_qa,
                    and_(
                        Review.status == ReviewStatusEnum.PENDING,
                        Review.assigned_qa_id == f_qa,
                    ),
                )
            )
    if f_case_type:
        stmt = stmt.where(Review.case_type == CaseTypeEnum(f_case_type))
    if f_case_number:
        stmt = stmt.where(Review.case_number.ilike(f"%{f_case_number}%"))

    reviews = list(
        (
            await db.execute(stmt.order_by(Review.created_at.desc()))
        )
        .scalars()
        .all()
    )

    nicknames = await _nickname_map(
        db,
        {
            rid
            for review in reviews
            for rid in (
                review.qa_id,
                review.support_agent_id,
                review.assigned_qa_id,
                review.created_by,
            )
        },
    )

    rows = [
        {
            "id": review.id,
            "case_type": review.case_type,
            "case_number": review.case_number,
            "final_score": review.final_score,
            "created_at": review.created_at,
            "notes": review.notes,
            "status": review.status.value,
            "agent_name": nicknames.get(review.support_agent_id, "Unknown"),
            "reviewer_name": nicknames.get(review.qa_id, "Unknown"),
            "creator_name": nicknames.get(
                review.created_by, nicknames.get(review.qa_id, "Unknown")
            ),
            "can_complete": (
                review.status == ReviewStatusEnum.PENDING
                and review.assigned_qa_id == auth.id
            ),
            "can_edit": performed_by_me,
        }
        for review in reviews
    ]

    has_filters = bool(
        f_agent or f_qa or f_case_type or f_case_number
    )

    return templates.TemplateResponse(
        request=request,
        name="partials/my_reviews.html",
        context={
            "rows": rows,
            "perspective": "reviewer" if performed_by_me else "agent",
            "title": "All Reviews" if scope == "all" else "My Reviews",
            "subtitle": (
                "Every case this period"
                if scope == "all"
                else (
                    "Reviews you performed"
                    if performed_by_me
                    else "Reviews about you"
                )
            ),
            "endpoint": (
                "/partials/all-reviews" if scope == "all"
                else "/partials/my-reviews"
            ),
            "period_range": _period_range_label(period_start, period_end),
            "filters": {
                "agents": [
                    {"id": user.id, "name": user.nickname}
                    for user in await _support_agents(db)
                ],
                "qas": [
                    {"id": qa.id, "name": qa.nickname} for qa in await _qas(db)
                ],
                "case_types": [
                    case_type.value for case_type in CaseTypeEnum
                ],
                "agent": f_agent,
                "qa": f_qa,
                "case_type": f_case_type,
                "case_number": f_case_number,
                "has_active": has_filters,
            },
        },
    )


@router.get(
    "/partials/to-review",
    name="partial_to_review",
    summary="To Review partial",
    include_in_schema=False,
)
async def partial_to_review(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the 'To review' HTMX partial for the assigned QA.

    Lists TWO groups of OPEN handoffs (status='pending', not
    soft-deleted), newest first within each: (a) rows ASSIGNED to the
    caller via ``assigned_qa_id`` and (b) the UNASSIGNED shared queue
    (``assigned_qa_id`` IS NULL) any reviewer may grab. Each row is
    completable straight from the list; completing a grabbed shared
    row keeps ``assigned_qa_id`` null (audit trail) while the
    completer becomes the reviewer of record. Reviewer roles only —
    Support-only users get a bare 403 like the other aggregate
    partials. Delegator names are batch-resolved via the nickname map
    (``qa_id`` first, ``created_by`` fallback).
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    if not is_reviewer(auth):
        return _forbidden()

    open_filters = (
        Review.status == ReviewStatusEnum.PENDING,
        Review.deleted_at.is_(None),
    )
    assigned_reviews = list(
        (
            await db.execute(
                select(Review)
                .where(Review.assigned_qa_id == auth.id, *open_filters)
                .order_by(Review.created_at.desc())
            )
        ).scalars().all()
    )
    shared_reviews = list(
        (
            await db.execute(
                select(Review)
                .where(Review.assigned_qa_id.is_(None), *open_filters)
                .order_by(Review.created_at.desc())
            )
        ).scalars().all()
    )

    reviews = assigned_reviews + shared_reviews
    nicknames = await _nickname_map(
        db,
        {
            rid
            for review in reviews
            for rid in (
                review.qa_id,
                review.created_by,
                review.support_agent_id,
                review.assigned_qa_id,
            )
        },
    )

    def _row(review: Review, unassigned: bool) -> dict[str, object]:
        return {
            "id": review.id,
            "created_at": review.created_at,
            "case_type": review.case_type,
            "case_number": review.case_number,
            "support_agent_name": (
                nicknames.get(review.support_agent_id) or "Unknown"
            ),
            "delegated_by_name": (
                nicknames.get(review.qa_id)
                or nicknames.get(review.created_by)
                or "Unknown"
            ),
            "assigned_qa_name": nicknames.get(review.assigned_qa_id),
            "unassigned": unassigned,
        }

    rows = [_row(review, False) for review in assigned_reviews] + [
        _row(review, True) for review in shared_reviews
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/to_review.html",
        context={"rows": rows},
    )


@router.get(
    "/partials/team-quotas",
    name="partial_team_quotas",
    summary="Team Quotas partial",
    include_in_schema=False,
)
async def partial_team_quotas(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the 'Team Quotas' HTMX partial for authenticated users.

    Context: every QA analyst with their quota-compliance totals
    (``{"id", "name", "completed", "required", "deficit"}``) for the
    current REPORTING period (26th→25th, named after the closing
    month). Global aggregate data: reviewer roles only — Support-only
    users get a bare 403 (no redirect). The per-QA compliance lookups
    are N+1 queries — acceptable for a small team.

    Supervisors/Admins additionally get the assignment-management
    context (``can_manage`` + grouped QAAssignment rows + the QA /
    Support user lists feeding the add form); the template hides the
    whole block server-side for everyone else.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    if not is_reviewer(auth):
        return _forbidden()

    period_start, period_end, closing_year, closing_month = (
        reporting_period.reporting_period_for(datetime.now(timezone.utc))
    )
    rows = []
    for qa in await _qas(db):
        compliance = await quota_service.get_qa_compliance(
            qa.id, closing_year, closing_month, db
        )
        rows.append(
            {
                "id": qa.id,
                "name": qa.nickname,
                "completed": compliance["total_completed"],
                "required": compliance["total_required"],
                "deficit": compliance["total_deficit"],
            }
        )

    can_manage = auth.has_role(RoleEnum.SUPERVISOR, RoleEnum.ADMIN)
    context: dict[str, object] = {
        "rows": rows,
        "period_label": reporting_period.period_label(
            closing_year, closing_month
        ),
        "period_range": _period_range_label(period_start, period_end),
        "can_manage": can_manage,
    }
    if can_manage:
        groups = await _assignment_groups(db)
        context.update(
            {
                "assignment_groups": groups,
                "assignment_count": sum(
                    len(group["assignments"]) for group in groups
                ),
                "qa_users": [
                    {"id": qa.id, "name": qa.nickname} for qa in await _qas(db)
                ],
                "support_agents": [
                    {"id": agent.id, "name": agent.nickname}
                    for agent in await _support_agents(db)
                ],
                # Delegating absence tracking ('No Cases') makes no sense —
                # assignments scope real review work only.
                "assignable_case_types": [
                    case_type.value
                    for case_type in CaseTypeEnum
                    if case_type is not CaseTypeEnum.NO_CASES
                ],
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/team_quotas.html",
        context=context,
    )


async def _assignment_groups(db: AsyncSession) -> list[dict[str, object]]:
    """QAAssignment rows grouped per QA for the management block.

    Each group is ``{"qa_id", "qa_name", "assignments": [{"id", "label"}]}``
    where ``label`` renders the assignment target ("Agent", "Case Type"
    or "Agent · Case Type"). Display names are materialized in Python
    via :func:`_nickname_map` (nickname is not a DB column).
    """
    assignments = list(
        (
            await db.execute(
                select(QAAssignment).order_by(
                    QAAssignment.qa_id, QAAssignment.created_at
                )
            )
        ).scalars().all()
    )
    nicknames = await _nickname_map(
        db,
        {
            rid
            for assignment in assignments
            for rid in (assignment.qa_id, assignment.support_agent_id)
        },
    )

    grouped: dict[int, list[dict[str, object]]] = {}
    for assignment in assignments:
        agent_name = nicknames.get(assignment.support_agent_id)
        case_type = (
            assignment.specialized_case_type.value
            if assignment.specialized_case_type is not None
            else None
        )
        if agent_name and case_type:
            label = f"{agent_name} · {case_type}"
        else:
            label = agent_name or case_type or f"#{assignment.id}"
        grouped.setdefault(assignment.qa_id, []).append(
            {"id": assignment.id, "label": label}
        )

    return [
        {
            "qa_id": qa_id,
            "qa_name": nicknames.get(qa_id, f"QA {qa_id}"),
            "assignments": rows,
        }
        for qa_id, rows in grouped.items()
    ]


async def _users_with_role(db: AsyncSession, role: RoleEnum) -> list[User]:
    """All ACTIVE users holding `role`, ordered by name.

    Soft-deleted users are excluded (``User.active_filter``) so they
    vanish from every "add to new work" surface while their historical
    reviews keep resolving their name via the nickname maps.
    """
    result = await db.execute(
        select(User)
        .join(UserRole, User.id == UserRole.user_id)
        .where(UserRole.role == role, User.active_filter())
        .order_by(User.name)
    )
    return list(result.scalars().all())


async def _support_agents(db: AsyncSession) -> list[User]:
    return await _users_with_role(db, RoleEnum.SUPPORT)


async def _qas(db: AsyncSession) -> list[User]:
    return await _users_with_role(db, RoleEnum.QA)


def _forbidden() -> PlainTextResponse:
    """Bare 403 for HTMX swaps (no redirect: HTMX would follow it)."""
    return PlainTextResponse("Forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.get(
    "/partials/qa-matrix",
    name="partial_qa_matrix",
    summary="QA Matrix partial",
    include_in_schema=False,
)
async def partial_qa_matrix(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the 'QA Matrix' HTMX partial for authenticated users.

    Context: every Support agent with their current REPORTING-period
    quota (26th→25th, named after the closing month) and their
    lifetime average final score as
    ``{"id", "name", "completed", "target", "avg_score"}`` (avg_score
    is None when the agent has no scored reviews). Global data:
    reviewer roles only — Support-only users get a bare 403 (no
    redirect). The per-agent quota lookups are N+1 queries —
    acceptable for a small team; averages are one grouped query.

    Soft-deleted rows are excluded from the listing AND from the
    average (pending rows never skewed either: their final_score is
    null). Case entries carry ``status`` plus the delegated-handoff
    audit fields so the template can render Pending chips and tooltips.
    Supervisors/Admins additionally get ``can_delegate`` + the QA list
    for the delegate-case modal.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    if not is_reviewer(auth):
        return _forbidden()

    period_start, period_end, closing_year, closing_month = (
        reporting_period.reporting_period_for(datetime.now(timezone.utc))
    )
    support_users = await _support_agents(db)
    agent_ids = [user.id for user in support_users]
    avgs: dict[int, float] = {}
    if agent_ids:
        result = await db.execute(
            select(Review.support_agent_id, func.avg(Review.final_score))
            .where(
                Review.support_agent_id.in_(agent_ids),
                Review.final_score.is_not(None),
                Review.deleted_at.is_(None),
            )
            .group_by(Review.support_agent_id)
        )
        avgs = {agent_id: round(float(avg), 1) for agent_id, avg in result.all()}

    reviews = list(
        (
            await db.execute(
                select(Review)
                .where(Review.deleted_at.is_(None))
                .order_by(Review.created_at.desc())
            )
        ).scalars().all()
    )

    nicknames = await _nickname_map(
        db,
        {
            rid
            for review in reviews
            for rid in (
                review.qa_id,
                review.support_agent_id,
                review.assigned_qa_id,
                review.created_by,
            )
        },
    )

    agents = []
    reviews_by_agent: dict[int, list[Review]] = {}
    for review in reviews:
        if review.support_agent_id is not None:
            reviews_by_agent.setdefault(review.support_agent_id, []).append(
                review
            )
    for user in support_users:
        quota = await quota_service.get_agent_quota(
            user.id, closing_year, closing_month, db
        )
        latest = reviews_by_agent.get(user.id, [])[: quota["target"]]
        agents.append(
            {
                "id": user.id,
                "name": user.nickname,
                "completed": quota["completed"],
                "target": quota["target"],
                "avg_score": avgs.get(user.id),
                "cases": [
                    {
                        "id": review.id,
                        "case_type": review.case_type,
                        "case_number": review.case_number,
                        "final_score": review.final_score,
                        "created_at": review.created_at,
                        "status": review.status.value,
                        "assigned_qa_name": nicknames.get(review.assigned_qa_id),
                        "reviewer_name": nicknames.get(review.qa_id),
                    }
                    for review in reversed(latest)
                ],
            }
        )

    cases = [
        {
            "id": review.id,
            "created_at": review.created_at,
            "case_type": review.case_type,
            "case_number": review.case_number,
            "final_score": review.final_score,
            "notes": review.notes,
            "status": review.status.value,
            "agent_name": nicknames.get(review.support_agent_id, "Unknown"),
            "reviewer_name": nicknames.get(review.qa_id, "Unknown"),
            "assigned_qa_name": nicknames.get(review.assigned_qa_id),
            "creator_name": nicknames.get(
                review.created_by, nicknames.get(review.qa_id, "Unknown")
            ),
        }
        for review in reviews
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/qa_matrix.html",
        context={
            "agents": agents,
            "cases": cases,
            "period_label": reporting_period.period_label(
                closing_year, closing_month
            ),
            "period_range": _period_range_label(period_start, period_end),
            "can_delegate": auth.has_role(RoleEnum.SUPERVISOR, RoleEnum.ADMIN),
            "qas": [
                {"id": qa.id, "name": qa.nickname} for qa in await _qas(db)
            ],
            "support_users": [
                {"id": user.id, "name": user.nickname} for user in support_users
            ],
            # Delegation targets one specific real case — 'No Cases' is
            # rejected by the API anyway.
            "delegate_case_types": [
                case_type.value
                for case_type in CaseTypeEnum
                if case_type is not CaseTypeEnum.NO_CASES
            ],
        },
    )


@router.get(
    "/partials/review-drawer",
    name="partial_review_drawer",
    summary="Review Drawer partial",
    include_in_schema=False,
)
async def partial_review_drawer(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Render the 'Review Drawer' HTMX partial for authenticated users.

    Context: the Support agents (``{"id", "name"}``) selectable as
    review targets, the QA analysts (``{"id", "name"}``) selectable as
    delegate assignees, and the case type values for the case-type
    picker.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    agents = [
        {"id": user.id, "name": user.nickname} for user in await _support_agents(db)
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/review_drawer.html",
        context={
            "agents": agents,
            "qas": [{"id": qa.id, "name": qa.nickname} for qa in await _qas(db)],
            "case_types": [case_type.value for case_type in CaseTypeEnum],
        },
    )


@router.get(
    "/partials/case-rules",
    name="partial_case_rules",
    summary="Case rules partial",
    include_in_schema=False,
)
async def partial_case_rules(
    request: Request,
    auth: PageUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    case_type: str = Query(...),
) -> Response:
    """Render the checkbox rules for one case type (HTMX fragment).

    Swapped into the review drawer when the case-type picker changes.
    ``rules`` is the active-rules item list (None for 'No Cases', which
    has no scorecard by definition); unknown case types are a bare 404.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    try:
        case_type_enum = CaseTypeEnum(case_type)
    except ValueError:
        return PlainTextResponse(
            "Not Found", status_code=status.HTTP_404_NOT_FOUND
        )

    if case_type_enum is CaseTypeEnum.NO_CASES:
        return templates.TemplateResponse(
            request=request,
            name="partials/case_rules.html",
            context={"rules": None},
        )

    rules = await scorecard_service.get_active_rules(case_type_enum, db)
    return templates.TemplateResponse(
        request=request,
        name="partials/case_rules.html",
        context={"rules": rules["items"]},
    )
