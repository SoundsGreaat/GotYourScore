"""Public bug report request schema."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BugReportInput(BaseModel):
    """Validated, bounded text-only bug report payload."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    title: str = Field(max_length=120)
    steps: str = Field(max_length=2500)
    actual: str = Field(max_length=1500)
    expected: str = Field(max_length=1500)
    environment: str | None = Field(default=None, max_length=250)
    context: str | None = Field(default=None, max_length=1500)

    @field_validator("title", "steps", "actual", "expected")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("A non-blank value without null characters is required.")
        return value.strip()

    @field_validator("environment", "context")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("Null characters are not permitted.")
        return value.strip() or None
