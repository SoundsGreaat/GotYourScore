"""Server-rendered HTML pages (Jinja2).

- GET /login: public login page; visitors holding a valid session
  are redirected straight to the dashboard.
- GET /: dashboard; unauthenticated visitors are redirected to
  /login instead of receiving a 401 JSON error.
- GET /partials/my-reviews, GET /partials/team-quotas,
  GET /partials/qa-matrix and GET /partials/review-drawer: small HTML
  fragments swapped into the dashboard by HTMX; protected like the
  dashboard (303 redirect to /login when unauthenticated).

RBAC: the global aggregate partials (qa-matrix, team-quotas) are
reviewer-only — Support-only users receive a bare 403 response (HTMX
surfaces it; no redirect). my-reviews is personal and review-drawer is
left accessible for all authenticated users.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import (
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.security import get_current_user, is_reviewer
from app.db.database import AsyncSession, get_db
from app.models import CaseTypeEnum, Review, RoleEnum, User, UserRole
from app.services import quota_service, reporting_period

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
async def dashboard(request: Request, auth: PageUser) -> Response:
    """Render the dashboard for authenticated users.

    The template hides reviewer chrome (New Review button, tracker
    tabs, aggregate sidebar entries) from Support-only users; the
    server-side RBAC on the partials stays the source of truth.
    """
    if isinstance(auth, RedirectResponse):
        return auth
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": auth,
            "is_support_only": auth.is_support_only,
            "can_review": auth.has_role(
                RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN
            ),
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
    """Render the 'My Reviews' HTMX partial for authenticated users.

    Perspective depends on roles: reviewer roles (QA/Supervisor/Admin)
    see the reviews THEY performed; everyone else sees the reviews
    performed ABOUT them. Both views are personal, so no RBAC gate
    beyond authentication. Latest 50 reviews; counterpart display
    names are batch-loaded with a single in_() query (no N+1).
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    performed_by_me = auth.has_role(
        RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN
    )
    stmt = select(Review).where(
        Review.qa_id == auth.id if performed_by_me else Review.support_agent_id == auth.id
    )
    reviews = list(
        (await db.execute(stmt.order_by(Review.created_at.desc()).limit(50)))
        .scalars()
        .all()
    )

    counterpart_ids = sorted(
        {
            review.support_agent_id if performed_by_me else review.qa_id
            for review in reviews
        }
    )
    names: dict[int, str] = {}
    if counterpart_ids:
        people = (
            await db.execute(select(User).where(User.id.in_(counterpart_ids)))
        ).scalars().all()
        names = {person.id: person.name for person in people}

    rows = [
        {
            "id": review.id,
            "case_type": review.case_type,
            "case_number": review.case_number,
            "final_score": review.final_score,
            "created_at": review.created_at,
            "notes": review.notes,
            "person_name": names.get(
                review.support_agent_id if performed_by_me else review.qa_id,
                "Unknown",
            ),
        }
        for review in reviews
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/my_reviews.html",
        context={
            "rows": rows,
            "perspective": "reviewer" if performed_by_me else "agent",
        },
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
                "name": qa.name,
                "completed": compliance["total_completed"],
                "required": compliance["total_required"],
                "deficit": compliance["total_deficit"],
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/team_quotas.html",
        context={
            "rows": rows,
            "period_label": reporting_period.period_label(
                closing_year, closing_month
            ),
            "period_range": _period_range_label(period_start, period_end),
        },
    )


async def _support_agents(db: AsyncSession) -> list[User]:
    """All users holding the Support role, ordered by name."""
    result = await db.execute(
        select(User)
        .join(UserRole, User.id == UserRole.user_id)
        .where(UserRole.role == RoleEnum.SUPPORT)
        .order_by(User.name)
    )
    return list(result.scalars().all())


async def _qas(db: AsyncSession) -> list[User]:
    """All users holding the QA role, ordered by name."""
    result = await db.execute(
        select(User)
        .join(UserRole, User.id == UserRole.user_id)
        .where(UserRole.role == RoleEnum.QA)
        .order_by(User.name)
    )
    return list(result.scalars().all())


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
    quota (26th→25th, named after the closing month) as
    ``{"id", "name", "completed", "target"}``. Global data: reviewer
    roles only — Support-only users get a bare 403 (no redirect). The
    per-agent quota lookups are N+1 queries — acceptable for a small
    team.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    if not is_reviewer(auth):
        return _forbidden()

    period_start, period_end, closing_year, closing_month = (
        reporting_period.reporting_period_for(datetime.now(timezone.utc))
    )
    agents = []
    for user in await _support_agents(db):
        quota = await quota_service.get_agent_quota(
            user.id, closing_year, closing_month, db
        )
        agents.append(
            {
                "id": user.id,
                "name": user.name,
                "completed": quota["completed"],
                "target": quota["target"],
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/qa_matrix.html",
        context={
            "agents": agents,
            "period_label": reporting_period.period_label(
                closing_year, closing_month
            ),
            "period_range": _period_range_label(period_start, period_end),
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
    review targets and the case type values for the case-type picker.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    agents = [
        {"id": user.id, "name": user.name} for user in await _support_agents(db)
    ]

    return templates.TemplateResponse(
        request=request,
        name="partials/review_drawer.html",
        context={
            "agents": agents,
            "case_types": [
                # "No Cases" reviews have null scores by definition and
                # are submitted manually — not offered in the drawer.
                case_type.value
                for case_type in CaseTypeEnum
                if case_type is not CaseTypeEnum.NO_CASES
            ],
        },
    )
