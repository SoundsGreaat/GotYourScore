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
    SERVICE_REQUEST = "Service Request"
    SECURITY_FIX = "Security Fix"
    INCIDENT_FIX = "Incident Fix"
    NO_CASES = "No Cases"


class ReviewStatusEnum(str, enum.Enum):
    """Lifecycle of a Review row.

    ``pending`` marks a delegated handoff: a Supervisor/Admin opened the
    review for a real case and handed it to a specific QA, but no
    scorecard exists yet. Pending rows carry null ``scorecard_data`` /
    ``final_score`` and do NOT count towards the support agent's quota
    until completed. ``completed`` is the normal, fully-scored state
    (and the implicit state of every row saved before delegation
    existed — enforced as the column's server default).
    """

    PENDING = "pending"
    COMPLETED = "completed"


class RoleEnum(str, enum.Enum):
    """User roles in the QA workflow."""

    ADMIN = "Admin"
    SUPERVISOR = "Supervisor"
    QA = "QA"
    SUPPORT = "Support"
    SALES = "Sales"
