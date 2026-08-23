"""Review ORM model.

A Review is a QA evaluation of a support agent's case:
- ``case_type``: the case type reviewed, or ``No Cases`` (edge case where
  the agent had no cases that month — scores are null but the review
  still counts towards the monthly quota of 6).
- ``scorecard_data``: JSONB with numerical deductions and multipliers
  for repeated errors.
- ``final_score``: computed final score (null for "No Cases").

Lifecycle / audit columns:
- ``status``: ``pending`` (delegated handoff awaiting its assigned QA)
  or ``completed`` (fully scored; server default so pre-existing rows
  and plain creations stay completed without extra code).
- ``assigned_qa_id``: the QA a pending review was delegated to; kept
  after completion as an audit trail (the completer becomes ``qa_id``).
- ``created_by``: who opened the row (``= qa_id`` for self-created
  reviews, the delegating Supervisor/Admin for pending ones); never
  rewritten.
- ``deleted_at``: soft-delete timestamp — soft-deleted rows are hidden
  from the API and never count towards quotas, but remain in the DB.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CaseTypeEnum, ReviewStatusEnum


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
    # Optional reference to the reviewed case in the ticketing system.
    case_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scorecard_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whole numbers only: deductions, multipliers and the final score
    # are integers per business rules.
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lifecycle: 'pending' (delegated handoff) or 'completed'. Server
    # default keeps plain creations and pre-existing rows completed.
    status: Mapped[ReviewStatusEnum] = mapped_column(
        SAEnum(
            ReviewStatusEnum,
            native_enum=False,
            length=20,
            validate_strings=True,
            # Persist enum *values* ("pending", "completed") not names.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=ReviewStatusEnum.COMPLETED.value,
    )
    # QA the review was delegated to (pending handoffs); kept untouched
    # after completion as the audit trail of who was asked.
    assigned_qa_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Who created the row: equals qa_id for self-created reviews, the
    # delegating Supervisor/Admin for pending ones. Never rewritten.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Soft-delete timestamp. Soft-deleted rows are hidden from the API
    # and excluded from quota/compliance counts; no index needed (only
    # single-row lookups test it).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Indexed: monthly quota queries filter on created_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Fetch server-side defaults (created_at, status) with INSERT ...
    # RETURNING so they are available after commit without a lazy
    # (sync) load.
    __mapper_args__ = {"eager_defaults": True}

    support_agent: Mapped["app.models.user.User"] = relationship(
        back_populates="reviews",
        foreign_keys=[support_agent_id],
    )
    qa: Mapped["app.models.user.User"] = relationship(
        foreign_keys=[qa_id],
    )
    assigned_qa: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[assigned_qa_id],
    )
    creator: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[created_by],
    )
