"""QAAssignment Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.enums import CaseTypeEnum


class QAAssignmentBase(BaseModel):
    """Shared assignment fields.

    At least one of ``support_agent_id`` (General assignment) or
    ``specialized_case_type`` (Specialized assignment) must be
    provided; providing both creates a Hybrid assignment (the QA is
    scoped to one agent AND one case type).
    """

    model_config = ConfigDict(extra="forbid")

    qa_id: int
    support_agent_id: int | None = None
    specialized_case_type: CaseTypeEnum | None = None

    @model_validator(mode="after")
    def validate_at_least_one_target(self) -> "QAAssignmentBase":
        """Ensure the assignment targets an agent, a case type, or both."""
        if self.support_agent_id is None and self.specialized_case_type is None:
            raise ValueError(
                "At least one of 'support_agent_id' (General) or "
                "'specialized_case_type' (Specialized) must be provided."
            )
        return self


class QAAssignmentCreate(QAAssignmentBase):
    """Payload for creating a QA assignment."""


class QAAssignmentRead(QAAssignmentBase):
    """Assignment representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
