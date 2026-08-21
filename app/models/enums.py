"""Enums shared across models and schemas.

Both enums subclass ``str`` so JSON serialization works naturally
(the value IS the string).
"""

import enum


class CaseTypeEnum(str, enum.Enum):
    """Case types a QA review can target.

    ``NO_CASES`` is the edge case: a Review submitted with this type has
    null scores but still counts towards the monthly quota.
    """

    INITIAL_FIX = "Initial Fix"
    SCHEDULED_FIX = "Scheduled Fix"
    SERVICE_REQUEST = "Service Request"
    SECURITY_FIX = "Security Fix"
    INCIDENT_FIX = "Incident Fix"
    NO_CASES = "No Cases"


class RoleEnum(str, enum.Enum):
    """User roles in the QA workflow."""

    ADMIN = "Admin"
    SUPERVISOR = "Supervisor"
    QA = "QA"
    SUPPORT = "Support"
