"""Models package: re-exports all ORM models and enums.

Importing this package ensures every model module is loaded so that
``Base.metadata`` (and therefore Alembic autogenerate) sees all tables.
"""

from app.models.enums import CaseTypeEnum, RoleEnum
from app.models.assignment import QAAssignment
from app.models.review import Review
from app.models.user import User

__all__ = [
    "CaseTypeEnum",
    "RoleEnum",
    "QAAssignment",
    "Review",
    "User",
]
