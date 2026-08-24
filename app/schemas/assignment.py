"""QAAssignment Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class QAAssignmentBase(BaseModel):
    """Shared assignment fields: a QA plus the Support agent whose
    reviews they are staffed to cover."""

    model_config = ConfigDict(extra="forbid")

    qa_id: int
    support_agent_id: int


class QAAssignmentCreate(QAAssignmentBase):
    """Payload for creating a QA assignment."""


class QAAssignmentRead(QAAssignmentBase):
    """Assignment representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
