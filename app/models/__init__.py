"""Models package: re-exports all ORM models and enums.

Importing this package ensures every model module is loaded so that
``Base.metadata`` (and therefore Alembic autogenerate) sees all tables.
"""

from app.models.app_setting import AppSetting
from app.models.bad_feedback import BadFeedback, BadFeedbackAgent
from app.models.enums import (
    AgentKindEnum,
    CaseTypeEnum,
    FaultEnum,
    ReviewStatusEnum,
    RoleEnum,
)
from app.models.assignment import QAAssignment
from app.models.review import Review
from app.models.scorecard import ScorecardItem, ScorecardTemplate
from app.models.system_prompt import SystemPrompt
from app.models.user import User, UserRole

__all__ = [
    "AgentKindEnum",
    "AppSetting",
    "BadFeedback",
    "BadFeedbackAgent",
    "CaseTypeEnum",
    "FaultEnum",
    "ReviewStatusEnum",
    "RoleEnum",
    "QAAssignment",
    "Review",
    "ScorecardItem",
    "ScorecardTemplate",
    "SystemPrompt",
    "User",
    "UserRole",
]
