"""Scorecard template ORM models.

A ``ScorecardTemplate`` groups the scoring rules for one case type:
each ``ScorecardItem`` is an error type with a fixed penalty. The AI
auto-scoring prompt is built from the active items of the template
matching the reviewed case type (see ``app.services.ai_service``).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CaseTypeEnum


class ScorecardTemplate(Base):
    """A named set of scoring rules (items) for one case type."""

    __tablename__ = "scorecard_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    case_type: Mapped[CaseTypeEnum] = mapped_column(
        SAEnum(
            CaseTypeEnum,
            native_enum=False,
            length=50,
            validate_strings=True,
            # Persist enum *values* ("Initial Fix", "No Cases", ...) instead of names.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Fetch server-side defaults (created_at) with INSERT ... RETURNING
    # so they are available after commit without a lazy (sync) load.
    __mapper_args__ = {"eager_defaults": True}

    items: Mapped[list["ScorecardItem"]] = relationship(
        backref="template",
        order_by="ScorecardItem.error_name",
        cascade="all, delete-orphan",
        # Let the DB-level ON DELETE CASCADE do the work instead of
        # loading every item just to delete it.
        passive_deletes=True,
    )


class ScorecardItem(Base):
    """One scoring rule: an error type and its penalty points."""

    __tablename__ = "scorecard_items"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "error_name", name="uq_scorecard_items_template_error"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("scorecard_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # snake_case key used in scorecards, e.g. "late_response".
    error_name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    penalty_points: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
