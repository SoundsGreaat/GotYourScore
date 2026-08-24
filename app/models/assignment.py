"""QAAssignment ORM model.

A Supervisor assigns a Support Agent to a QA reviewer (General scope):
``support_agent_id`` is set and the QA owns that agent's reporting-
period quota (see ``app.services.quota_service``).

An agent is staffed to AT MOST ONE QA at any time — enforced at the DB
level via a UNIQUE constraint on ``support_agent_id``; moving an agent
to another QA is a delete+recreate pair, never a second row.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class QAAssignment(Base):
    """Assignment of a Support agent to a QA reviewer."""

    __tablename__ = "qa_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    qa_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    support_agent_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
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
    support_agent: Mapped["User"] = relationship(
        foreign_keys=[support_agent_id],
        backref="support_assignments",
    )
