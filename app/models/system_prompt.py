"""SystemPrompt ORM model.

DB-managed LLM system prompts, addressed by a string ``key`` (e.g.
``"ai_scoring"``). Business rules:

- Multiple rows may exist per key (versioning/history); at most one row
  per key should be active at a time. The uniqueness is enforced
  partially: the CRUD service deactivates the other active rows of the
  same key whenever a row is created/updated with ``is_active=true``.
- Resolvers pick the NEWEST active row per key (``created_at DESC,
  id DESC``) — see ``app.services.ai_service``.
- Nothing is seeded by migrations: an empty table means callers fall
  back to their hardcoded prompt constants.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemPrompt(Base):
    """A named, versioned LLM system prompt."""

    __tablename__ = "system_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Fetch server-side defaults with INSERT ... RETURNING so they are
    # available after commit without a lazy (sync) load.
    __mapper_args__ = {"eager_defaults": True}
