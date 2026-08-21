"""Application settings loaded from environment variables / .env file."""

from functools import lru_cache

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

    # Google OAuth domain restriction for the internal support team.
    ALLOWED_DOMAIN: str = "example.com"

    # Global QA quota: strictly 6 cases per support agent per month.
    # Calculated dynamically from the Review table; kept here for
    # validation and potential configurability.
    MONTHLY_QUOTA: int = 6


@lru_cache
def get_settings() -> Settings:
    """Return the cached application Settings instance."""
    return Settings()
