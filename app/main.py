"""GotYourScore FastAPI application entrypoint."""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.PROJECT_NAME)


@app.get("/ping")
async def ping() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "project": settings.PROJECT_NAME}
