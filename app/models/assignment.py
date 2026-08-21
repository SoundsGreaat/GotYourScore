"""QAAssignment ORM model.

A Supervisor assigns a QA:
- to a specific Support Agent (General): ``support_agent_id`` is set,
  ``specialized_case_type`` is NULL;
- to a specific Case Type across all agents (Specialized):
  ``specialized_case_type`` is set, ``support_agent_id`` is NULL;
- to a specific Support Agent AND Case Type (Hybrid): both are set —
  the QA is scoped to that agent for that case type.

At least one of the two must be set — enforced at the DB level via a
CHECK constraint using PostgreSQL's ``num_nonnulls``.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CaseTypeEnum

if TYPE_CHECKING:
    from app.models.user import User


class QAAssignment(Base):
    """Assignment of a QA to a support agent (General), a case type
    (Specialized), or both (Hybrid).
    """

    __tablename__ = "qa_assignments"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(support_agent_id, specialized_case_type) >= 1",
            name="ck_qa_assignments_at_least_one_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    qa_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    support_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    specialized_case_type: Mapped[CaseTypeEnum | None] = mapped_column(
        SAEnum(
            CaseTypeEnum,
            native_enum=False,
            length=50,
            validate_strings=True,
            # Persist enum *values* ("Initial Fix", "No Cases", ...) instead of names.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Fetch server-side defaults (created_at) with INSERT ... RETURNING
    # so they are available after commit without a lazy (sync) load.
    __mapper_args__ = {"eager_defaults": True}

    qa: Mapped["User"] = relationship(
        foreign_keys=[qa_id],
        backref="qa_assignments",
    )
    support_agent: Mapped["User | None"] = relationship(
        foreign_keys=[support_agent_id],
        backref="support_assignments",
    )
