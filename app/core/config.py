"""Application settings loaded from environment variables / .env file."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """GotYourScore application settings.

    Values are read from the environment or a local ``.env`` file
    (case-sensitive keys).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    PROJECT_NAME: str = "GotYourScore"

    # Async PostgreSQL DSN, e.g. postgresql+asyncpg://postgres:postgres@localhost:5432/gotyourscore
    DATABASE_URL: str

    OPENROUTER_API_KEY: str | None = None
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None

    # Secret used to sign session cookies (Starlette SessionMiddleware).
    # The default is an INSECURE dev-only value; override it in .env /
    # the environment with a strong random string in production.
    SECRET_KEY: str = "dev-insecure-secret-key-change-me"

    # Google OAuth domain restriction for the internal support team.
    ALLOWED_DOMAIN: str = "example.com"

    # Session cookie hardening: set SESSION_COOKIE_SECURE=true in production
    # (HTTPS-only cookies). SESSION_MAX_AGE bounds cookie lifetime in
    # seconds (None = browser-session cookie).
    SESSION_COOKIE_SECURE: bool = False
    SESSION_MAX_AGE: int | None = None

    # Global QA quota: strictly 6 cases per support agent per month.
    # Calculated dynamically from the Review table; kept here for
    # validation and potential configurability.
    MONTHLY_QUOTA: int = 6

    @field_validator(
        "OPENROUTER_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "SESSION_MAX_AGE",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: object) -> object:
        """Treat empty env values (e.g. ``KEY=`` in .env) as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached application Settings instance."""
    return Settings()
