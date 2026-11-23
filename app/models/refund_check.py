"""Refund Check ORM models.

Refund Check is an independent tracker with the same lifecycle and agent
cards as Bad Feedback. The tables are intentionally separate so its cases
and custom field values can never leak into the Bad Feedback workflow.
"""

from datetime import date as date_type
from datetime import datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AgentKindEnum, FaultEnum, ReviewStatusEnum


class RefundCheck(Base):
    """A Refund Check case (not a scored Review)."""

    __tablename__ = "refund_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    fb_date: Mapped[date_type | None] = mapped_column(Date, nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_case: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Custom values are held separately so the legacy columns remain
    # authoritative compatibility storage for the five default fields.
    field_values_data: Mapped[dict[str, str | None]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    qa_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    assigned_qa_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    qa_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    __mapper_args__ = {"eager_defaults": True}

    assigned_qa: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[assigned_qa_id]
    )
    qa: Mapped["app.models.user.User | None"] = relationship(foreign_keys=[qa_id])
    creator: Mapped["app.models.user.User | None"] = relationship(
        foreign_keys=[created_by]
    )
    agents: Mapped[list["RefundCheckAgent"]] = relationship(
        back_populates="feedback",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RefundCheckAgent.id",
    )

    @property
    def field_values(self) -> dict[str, str | None]:
        """Canonical case fields plus custom values, all string/null."""
        values: dict[str, str | None] = {
            "fb_date": self.fb_date.isoformat() if self.fb_date else None,
            "customer_info": self.customer_info,
            "source": self.source,
            "customer_feedback": self.customer_feedback,
            "related_case": self.related_case,
        }
        values.update(
            {
                key: value
                for key, value in (self.field_values_data or {}).items()
                if key not in values
            }
        )
        return values


class RefundCheckAgent(Base):
    """One involved Support or Sales agent on a Refund Check case."""

    __tablename__ = "refund_check_agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("refund_checks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    qa_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    feedback: Mapped["RefundCheck"] = relationship(back_populates="agents")
    user: Mapped["app.models.user.User"] = relationship(lazy="joined")
