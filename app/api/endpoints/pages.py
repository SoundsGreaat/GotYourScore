"""Server-rendered HTML pages (Jinja2).

- GET /login: public login page; visitors holding a valid session
  are redirected straight to the dashboard.
- GET /: dashboard; unauthenticated visitors are redirected to
  /login instead of receiving a 401 JSON error.
- GET /partials/my-reviews, GET /partials/team-quotas,
  GET /partials/qa-matrix and GET /partials/review-drawer: small HTML
  fragments swapped into the dashboard by HTMX; protected like the
  dashboard (303 redirect to /login when unauthenticated).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.security import get_current_user
from app.db.database import AsyncSession, get_db
from app.models import CaseTypeEnum, RoleEnum, User
from app.services import quota_service

router = APIRouter(tags=["pages"])

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
    """Render the dashboard for authenticated users."""
    if isinstance(auth, RedirectResponse):
        return auth
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"current_user": auth},
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
async def partial_my_reviews(request: Request, auth: PageUser) -> Response:
    """Render the 'My Reviews' HTMX partial for authenticated users.

    A bare HTML fragment (no base layout) swapped into the
    dashboard's #main-content container.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="partials/my_reviews.html",
        context={},
    )


@router.get(
    "/partials/team-quotas",
    name="partial_team_quotas",
    summary="Team Quotas partial",
    include_in_schema=False,
)
async def partial_team_quotas(request: Request, auth: PageUser) -> Response:
    """Render the 'Team Quotas' HTMX partial for authenticated users.

    A bare HTML fragment (no base layout) swapped into the
    dashboard's #main-content container.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="partials/team_quotas.html",
        context={},
    )


async def _support_agents(db: AsyncSession) -> list[User]:
    """All Support users ordered by name (for matrix/drawer contexts)."""
    result = await db.execute(
        select(User).where(User.role == RoleEnum.SUPPORT).order_by(User.name)
    )
    return list(result.scalars().all())


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

    Context: every Support agent with their current-month quota as
    ``{"id", "name", "completed", "target"}``. The per-agent quota
    lookups are N+1 queries — acceptable for a small team.
    """
    redirect = _htmx_redirect_if_needed(auth, request)
    if redirect is not None:
        return redirect

    now = datetime.now(timezone.utc)
    agents = []
    for user in await _support_agents(db):
        quota = await quota_service.get_agent_quota(
            user.id, now.year, now.month, db
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
        context={"agents": agents},
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
