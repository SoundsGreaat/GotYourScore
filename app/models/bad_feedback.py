"""Bad Feedback ORM models.

A Bad Feedback record is a customer complaint routed to QA from any
source (CSAT, app-store review, escalation...). Unlike a Review it has
no scorecard: it carries free-form context (date, source, customer
info/feedback, related case) plus a list of involved agents.

Involved agents live in ``bad_feedback_agents``: each row pairs a user
with the ``kind`` of involvement (Support or Sales) and the QA verdict
for that agent — ``fault`` plus an optional per-agent comment. Rows are
added/edited dynamically while editing the record, one comment+fault
pair per involved agent.

Lifecycle mirrors Review: ``pending`` (imported/delegated, awaiting
work) → ``completed`` (finished by a QA — the finisher becomes
``qa_id``). ``assigned_qa_id`` optionally routes a pending record to a
specific QA; null means the shared queue. Soft delete mirrors Review.
"""

from datetime import date as date_type
from datetime import datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    AgentKindEnum,
    FaultEnum,
    ReviewStatusEnum,
)


class BadFeedback(Base):
    """A customer-feedback QA case (not a scored Review)."""

    __tablename__ = "bad_feedbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Plain date (no timezone semantics); import fills the missing year
    # with the current one. Nullable — some rows arrive without a date.
    fb_date: Mapped[date_type | None] = mapped_column(
        Date, nullable=True, index=True
    )
    # Where the feedback came from (CSAT, app store, escalation, ...).
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # CRM link or customer email, verbatim from the sheet.
    customer_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The customer's own words; often missing.
    customer_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Related ticket/case reference; often missing.
    related_case: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # QA's cross-agent summary (list-level, not per-agent).
    qa_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle: 'pending' (imported, awaiting work) or 'completed'.
    status: Mapped[ReviewStatusEnum] = mapped_column(
        SAEnum(
            ReviewStatusEnum,
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=ReviewStatusEnum.PENDING.value,
    )

    # Optional QA the pending record is routed to; null = shared queue.
    assigned_qa_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The QA who completed the record (set once, on completion).
    qa_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # When the record was completed — the calendar-month grouping key
    # for the BF views' month selector (unlike QA Score's reporting
    # periods, BF months are plain calendar months of completion).
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Who imported / created the record.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Soft delete, same contract as Review.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Fetch server-side defaults with INSERT ... RETURNING.
    __mapper_args__ = {"eager_defaults": True}

    assigned_qa: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[assigned_qa_id]
    )
    qa: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[qa_id]
    )
    creator: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[created_by]
    )
    agents: Mapped[list["BadFeedbackAgent"]] = relationship(
        back_populates="feedback",
        cascade="all, delete-orphan",
        # Agent cards render with the user nickname everywhere.
        lazy="selectin",
        order_by="BadFeedbackAgent.id",
    )


class BadFeedbackAgent(Base):
    """One involved agent on a Bad Feedback record (Support or Sales)."""

    __tablename__ = "bad_feedback_agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("bad_feedbacks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which side the agent acted on for this feedback.
    kind: Mapped[AgentKindEnum] = mapped_column(
        SAEnum(
            AgentKindEnum,
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    # QA verdict for this agent; null while the record is still open.
    fault: Mapped[FaultEnum | None] = mapped_column(
        SAEnum(
            FaultEnum,
            native_enum=False,
            length=20,
            validate_strings=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )
    # Per-agent QA comment.
    qa_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    feedback: Mapped["BadFeedback"] = relationship(
        back_populates="agents"
    )
    # joined: agent cards serialize with their user label everywhere
    # (list + detail + import report) — a sync lazy load inside async
    # sessions would raise MissingGreenlet.
    user: Mapped["app.models.user.User"] = relationship(lazy="joined")
