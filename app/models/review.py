"""Review ORM model.

A Review is a QA evaluation of a support agent's case:
- ``case_type``: the case type reviewed, or ``No Cases`` (edge case where
  the agent had no cases that month — scores are null but the review
  still counts towards the monthly quota of 6).
- ``scorecard_data``: JSONB with numerical deductions and multipliers
  for repeated errors.
- ``final_score``: computed final score (null for "No Cases").
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CaseTypeEnum


class Review(Base):
    """A QA review of a support agent's case (or a 'No Cases' placeholder)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    support_agent_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qa_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    )
    scorecard_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whole numbers only: deductions, multipliers and the final score
    # are integers per business rules.
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Indexed: monthly quota queries filter on created_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Fetch server-side defaults (created_at) with INSERT ... RETURNING
    # so they are available after commit without a lazy (sync) load.
    __mapper_args__ = {"eager_defaults": True}

    support_agent: Mapped["app.models.user.User"] = relationship(
        back_populates="reviews",
        foreign_keys=[support_agent_id],
    )
    qa: Mapped["app.models.user.User"] = relationship(
        foreign_keys=[qa_id],
    )
