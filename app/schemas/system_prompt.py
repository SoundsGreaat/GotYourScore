"""SystemPrompt Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SystemPromptBase(BaseModel):
    """Shared prompt fields with key/content validation."""

    key: str = Field(
        pattern=r"^[a-z0-9_]{1,100}$",
        description="Prompt identifier, e.g. 'ai_scoring'.",
    )
    content: str = Field(min_length=1)
    is_active: bool = True


class SystemPromptCreate(SystemPromptBase):
    """Payload for creating a system prompt (strict)."""

    model_config = ConfigDict(extra="forbid")


class SystemPromptUpdate(BaseModel):
    """Partial update payload: only provided fields are applied."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class SystemPromptRead(SystemPromptBase):
    """System prompt representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
