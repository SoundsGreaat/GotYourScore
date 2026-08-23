"""User ORM model.

Roles are many-to-many via the ``user_roles`` table: a user holds one
or more :class:`~app.models.enums.RoleEnum` values (a Support+QA hybrid
is simultaneously a legitimate review target AND a reviewer). Role
checks must go through ``User.has_role`` / ``User.is_support_only`` —
never assume a single role.

Soft delete: ``deleted_at`` marks admin-removed accounts. Their
historical reviews stay fully intact, but they disappear from every
"add to new work" surface and are excluded from compliance assigned-
agent math (always compose queries with :meth:`User.active_filter`).
A soft-deleted account is also blocked from Google login.

Implementation note: the association uses a mapped ``UserRole``
association class + ``association_proxy`` instead of a bare
``sqlalchemy.Table`` secondary, because ``relationship()`` cannot target
an unmapped enum type directly (UnmappedClassError). The table shape is
identical to the plain-secondary design.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, DateTime, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import RoleEnum

if TYPE_CHECKING:
    from app.models.review import Review


class UserRole(Base):
    """One ``(user_id, role)`` membership row.

    Roles persist as VARCHAR holding RoleEnum *values* ("Admin", "QA",
    ...) so rows stay human-readable and match the legacy single-column
    values they were backfilled from.
    """

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
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
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"UserRole(user_id={self.user_id!r}, role={self.role!r})"


class User(Base):
    """Application user holding one or more roles."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    # Admin-created placeholder accounts have no name until their first
    # Google login syncs it.
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Soft-delete timestamp; deleted accounts keep their review history
    # but are excluded from new-work surfaces and blocked from login.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # selectin: role rows load eagerly alongside the user, avoiding sync
    # lazy-loads inside async sessions.
    _role_rows: Mapped[list["UserRole"]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Public view of the roles as plain RoleEnum values. creator is
    # required because the declarative __init__ is keyword-only.
    roles: AssociationProxy[list[RoleEnum]] = association_proxy(
        "_role_rows", "role", creator=lambda role: UserRole(role=role)
    )

    # Convenience relationships (kept simple).
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="support_agent",
        foreign_keys="Review.support_agent_id",
    )

    def has_role(self, *roles: RoleEnum) -> bool:
        """True when the user holds ANY of the given roles."""
        return any(role in self.roles for role in roles)

    @classmethod
    def active_filter(cls) -> ColumnElement[bool]:
        """SQLAlchemy criterion matching non-deleted users.

        Compose this into every query that feeds an "add to new work"
        surface or compliance assigned-agent math — soft-deleted users
        must be invisible there while their historical reviews remain.
        Uniqueness checks (emails are never reused) deliberately do NOT
        use this filter.
        """
        return cls.deleted_at.is_(None)

    @property
    def is_deleted(self) -> bool:
        """True once the account has been soft-deleted."""
        return self.deleted_at is not None

    @property
    def nickname(self) -> str:
        """Capitalized email local part — the team's working nickname.

        Falls back to the full name only when the email has no "@"
        (defensive; emails are unique and validated upstream). The name
        fallback also tolerates NULL (placeholder accounts).
        """
        if "@" in self.email:
            local = self.email.split("@", 1)[0]
        else:
            local = self.name or ""
        return local[:1].upper() + local[1:]

    @property
    def is_support_only(self) -> bool:
        """True when the user has SUPPORT but no elevated role."""
        return self.has_role(RoleEnum.SUPPORT) and not self.has_role(
            RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN
        )
