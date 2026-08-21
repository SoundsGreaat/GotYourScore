"""User Pydantic schemas."""

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.enums import RoleEnum


class UserBase(BaseModel):
    """Shared user fields (strict: unknown fields are rejected)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: RoleEnum
    name: str


class UserCreate(UserBase):
    """Payload for creating a user."""


class UserRead(UserBase):
    """User representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
