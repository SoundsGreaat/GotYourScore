"""GotYourScore FastAPI application entrypoint."""

import math
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.auth import router as auth_router
from app.api.endpoints.pages import router as pages_router
from app.api.endpoints.reviews import router as reviews_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.PROJECT_NAME)


def _sanitize_nonfinite(obj: object) -> object:
    """Replace NaN/Infinity floats (un-JSON-serializable) with strings."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, dict):
        return {key: _sanitize_nonfinite(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nonfinite(item) for item in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 handler that tolerates non-finite floats in the echoed input.

    Without this, a request containing a raw ``NaN``/``Infinity`` JSON
    literal would raise ``ValueError`` while serializing the validation
    error (which echoes the offending input) and surface as a 500.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_nonfinite(jsonable_encoder(exc.errors()))},
    )

# Signed HTTP-only cookie sessions (secret overridable via .env SECRET_KEY;
# set SESSION_COOKIE_SECURE=true behind HTTPS in production).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    https_only=settings.SESSION_COOKIE_SECURE,
    max_age=settings.SESSION_MAX_AGE,
)

# Static assets (CSS/JS) served under /static.
# Anchored to this file so static assets resolve regardless of the CWD
# the app is launched from.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(auth_router)
# Business endpoints live under /api (reviews: creation, quota, lookup).
app.include_router(reviews_router, prefix="/api")
# Server-rendered HTML pages (login, dashboard) at the application root.
app.include_router(pages_router)


@app.get("/ping")
async def ping() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "project": settings.PROJECT_NAME}
