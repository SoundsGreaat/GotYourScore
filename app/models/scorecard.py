"""Scorecard template ORM models.

A ``ScorecardTemplate`` groups the scoring rules for one case type:
each ``ScorecardItem`` is an error type with a fixed penalty. The AI
auto-scoring prompt is built from the active items of the ACTIVE
template matching the reviewed case type (see
``app.services.scorecard_service`` / ``app.services.ai_service``).
Deactivating a template or item only affects FUTURE reviews: saved
reviews embed a frozen snapshot of the rules that were active at save
time (historical immutability).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
    # ONE active template per case type: activating a template demotes
    # its siblings (same rule as SystemPrompt's one-active-row-per-key).
    __table_args__ = (
        Index(
            "uq_scorecard_templates_case_type_active",
            "case_type",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

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
    # Inactive templates are excluded from AI prompts and from the
    # rules snapshot embedded into newly saved reviews.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # Fetch server-side defaults (created_at) with INSERT ... RETURNING
    # so they are available after commit without a lazy (sync) load.
    __mapper_args__ = {"eager_defaults": True}

    items: Mapped[list["ScorecardItem"]] = relationship(
        backref="template",
        order_by="ScorecardItem.position",
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
    # Taxonomy label rendered as section headers in the AI scoring
    # prompt and used to group rules in the admin editor. Rows that
    # predate the column are backfilled with "General" by the server
    # default (see migration b4f8e2a6c9d1).
    category: Mapped[str] = mapped_column(
        String(200), nullable=False, default="General", server_default=text("'General'")
    )
    # Admin-defined ordering key (0-based): lower values render first
    # in the editor, the AI prompt and rules snapshots. Rows that
    # predate the column start at 0 (see migration d7e4b9a3f1c2).
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    penalty_points: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
