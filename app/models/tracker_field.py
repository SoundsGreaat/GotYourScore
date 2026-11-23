"""Configurable case fields for tracker workflows."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrackerField(Base):
    """A tracker-specific, stable case-field definition.

    Field keys are never changed or reused. Archiving only hides a
    definition from normal field listings; existing record values remain
    stored under the key and can be revealed again by restoring it.
    """

    __tablename__ = "tracker_fields"
    __table_args__ = (
        UniqueConstraint("tracker", "key", name="uq_tracker_fields_tracker_key"),
        CheckConstraint(
            "tracker IN ('bad-feedback', 'refund-check')",
            name="ck_tracker_fields_tracker",
        ),
        CheckConstraint(
            "field_type IN ('date', 'text', 'textarea')",
            name="ck_tracker_fields_type",
        ),
        CheckConstraint(
            "position >= 0 AND position <= 100000",
            name="ck_tracker_fields_position",
        ),
        Index(
            "ix_tracker_fields_tracker_archived_position",
            "tracker",
            "archived_at",
            "position",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tracker: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    show_in_my_reviews: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
