"""GotYourScore FastAPI application entrypoint."""

import logging
import time
from contextlib import suppress
from time import perf_counter
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.auth import router as auth_router
from app.api.endpoints.admin import router as admin_router
from app.api.endpoints.ai import router as ai_router
from app.api.endpoints.assignments import router as assignments_router
from app.api.endpoints.bad_feedback import (
    refund_check_router,
    router as bad_feedback_router,
)
from app.api.endpoints.pages import router as pages_router
from app.api.endpoints.reviews import router as reviews_router
from app.api.endpoints.system_prompts import router as system_prompts_router
from app.core.config import get_settings
from app.core.observability import (
    assign_request_id,
    configure_logging,
    log_event,
    request_failure_logged,
    safe_traceback,
)
from app.core.observability import request_id_var

settings = get_settings()
configure_logging(settings.APP_LOG_LEVEL, settings.APP_LOG_FILE)
logger = logging.getLogger(__name__)

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


_REJECTION_CODES = {
    400: "business_validation_error",
    401: "authentication_required",
    403: "authorization_denied",
    404: "resource_not_found",
    409: "resource_conflict",
    413: "request_too_large",
    429: "rate_limited",
}


class RequestObservabilityMiddleware:
    """Pure ASGI request boundary; catches errors without exposing them to Uvicorn."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id, token = assign_request_id()
        failure_token = request_failure_logged.set(False)
        started_at = perf_counter()
        status: int | None = None
        streaming = False
        finished = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, streaming, finished
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                streaming = "content-length" not in headers
            elif message["type"] == "http.response.body":
                finished = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except ClientDisconnect:
            pass
        except Exception as exc:
            route = getattr(scope.get("route"), "name", "unmatched")
            elapsed = round((perf_counter() - started_at) * 1000, 2)
            log_event(
                logger,
                "http.request.unhandled_error",
                "error",
                level=logging.ERROR,
                method=scope.get("method"),
                route=route,
                status_code=status if status is not None else 500,
                error_type=type(exc).__name__,
                safe_traceback=safe_traceback(exc),
                elapsed_ms=elapsed,
                reason_code="unhandled_exception",
            )
            with suppress(OSError):
                if status is None:
                    error = JSONResponse({"detail": "Internal server error."}, status_code=500)
                    await error(scope, receive, send_with_request_id)
                elif streaming and not finished:
                    # A stream has already sent headers. Terminate it without an
                    # exception body; the client requires a terminal success event.
                    await send({"type": "http.response.body", "body": b""})
        else:
            route_name = getattr(scope.get("route"), "name", None)
            if (
                route_name
                and status is not None
                and not request_failure_logged.get()
                and (status in _REJECTION_CODES or status >= 500)
            ):
                server_error = status >= 500
                log_event(
                    logger,
                    "http.request.completed",
                    "server_failure" if server_error else "expected_failure",
                    level=logging.ERROR if server_error else logging.WARNING,
                    method=scope.get("method"),
                    route=route_name,
                    status_code=status,
                    elapsed_ms=round((perf_counter() - started_at) * 1000, 2),
                    reason_code=_REJECTION_CODES.get(status, "http_server_error"),
                )
        finally:
            request_failure_logged.reset(failure_token)
            request_id_var.reset(token)


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
