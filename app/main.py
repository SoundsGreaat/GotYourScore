"""GotYourScore FastAPI application entrypoint."""

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.api.auth import router as auth_router
from app.api.endpoints.admin import router as admin_router
from app.api.endpoints.ai import router as ai_router
from app.api.endpoints.assignments import router as assignments_router
from app.api.endpoints.bad_feedback import (
    refund_check_router,
    router as bad_feedback_router,
)
from app.api.endpoints.bug_reports import router as bug_reports_router
from app.api.endpoints.pages import router as pages_router
from app.api.endpoints.reviews import router as reviews_router
from app.api.endpoints.system_prompts import router as system_prompts_router
from app.core.config import get_settings
from app.core.observability import configure_logging
from app.core.request_logging import RequestObservabilityMiddleware

settings = get_settings()
configure_logging(settings.APP_LOG_LEVEL, settings.APP_LOG_FILE)

app = FastAPI(title=settings.PROJECT_NAME)

# Error pages use the same base template and cache-busted assets as the
# application pages, but live here so they remain available when no route
# matches at all.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
error_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
error_templates.env.globals["asset_v"] = str(int(time.time()))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return validation metadata without reflecting submitted values."""
    safe_errors = [
        {
            "loc": list(error.get("loc", ()))[:1],
            "msg": "Invalid request.",
            "type": error.get("type", "validation_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": safe_errors},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_page_handler(
    request: Request, exc: StarletteHTTPException
):
    """Render the branded 404 for browser navigation without changing API errors."""
    if exc.status_code >= 500:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": "Internal server error."},
            headers=exc.headers,
        )
    accepts_html = "text/html" in request.headers.get("accept", "")
    if (
        exc.status_code == 404
        and request.method == "GET"
        and accepts_html
        and not request.url.path.startswith("/api/")
    ):
        return error_templates.TemplateResponse(
            request=request,
            name="404.html",
            status_code=404,
        )
    return await http_exception_handler(request, exc)


# Signed HTTP-only cookie sessions (secret overridable via .env SECRET_KEY;
# set SESSION_COOKIE_SECURE=true behind HTTPS in production).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    https_only=settings.SESSION_COOKIE_SECURE,
    max_age=settings.SESSION_MAX_AGE,
)
app.add_middleware(RequestObservabilityMiddleware)

# Static assets (CSS/JS) served under /static.
# Anchored to this file so static assets resolve regardless of the CWD
# the app is launched from.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(auth_router)
# Business endpoints live under /api (reviews: creation, delegation,
# editing, soft delete, quota, compliance, lookup; assignments:
# Supervisor/Admin QA staffing; ai: notes refactoring and preview
# scoring; system-prompts: Admin-only LLM prompt management).
app.include_router(reviews_router, prefix="/api")
app.include_router(assignments_router, prefix="/api")
app.include_router(ai_router, prefix="/api")
app.include_router(bug_reports_router, prefix="/api")
app.include_router(system_prompts_router, prefix="/api")
# Bad Feedback: complaint tracking with smart Excel import (QA and up).
app.include_router(bad_feedback_router, prefix="/api")
app.include_router(refund_check_router, prefix="/api")
# Server-rendered HTML pages (login, dashboard) at the application root.
app.include_router(pages_router)
# Admin panel pages (HTMX partials, Admin-only) at the application root.
app.include_router(admin_router)


@app.get("/ping")
async def ping() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "project": settings.PROJECT_NAME}
