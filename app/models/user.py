"""User ORM model."""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Enum as SAEnum

from app.db.base import Base
from app.models.enums import RoleEnum

if TYPE_CHECKING:
    from app.models.review import Review


class User(Base):
    """Application user: Admin, Supervisor, QA, or Support agent."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    role: Mapped[RoleEnum] = mapped_column(
        SAEnum(
            RoleEnum,
            native_enum=False,
            length=50,
            validate_strings=True,
            # Persist enum *values* ("Admin", "QA", ...) instead of names.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Convenience relationships (kept simple).
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="support_agent",
        foreign_keys="Review.support_agent_id",
    )
