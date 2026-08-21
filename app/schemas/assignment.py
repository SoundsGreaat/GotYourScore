"""QAAssignment Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.enums import CaseTypeEnum


class QAAssignmentBase(BaseModel):
    """Shared assignment fields.

    Exactly one of ``support_agent_id`` (General assignment) or
    ``specialized_case_type`` (Specialized assignment) must be provided.
    """

    model_config = ConfigDict(extra="forbid")

    qa_id: int
    support_agent_id: int | None = None
    specialized_case_type: CaseTypeEnum | None = None

    @model_validator(mode="after")
    def validate_exactly_one_target(self) -> "QAAssignmentBase":
        """Ensure the assignment is either General or Specialized, not both/neither."""
        if (self.support_agent_id is None) == (self.specialized_case_type is None):
            raise ValueError(
                "Exactly one of 'support_agent_id' (General) or "
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
