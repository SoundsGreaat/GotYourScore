"""AppSetting ORM model.

A tiny key/value store for runtime-tunable app configuration
(JSONB values), so settings can be edited from the Admin panel without
a redeploy. Business rules:

- One row per ``key`` (unique); resolvers read the row's ``value``
  directly and fall back to their hardcoded constants when the table
  has no row for their key.
- Nothing is seeded by migrations: an empty table means every setting
  uses its built-in default.

Current keys: ``"openrouter_provider"`` — OpenRouter provider routing; and
``"openrouter_request"`` — optional model and reasoning-effort overrides
(see ``app.services.ai_service``).
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    """A single named JSONB application setting."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
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
