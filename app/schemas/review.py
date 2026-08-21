"""Review Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.enums import CaseTypeEnum


class ReviewBase(BaseModel):
    """Shared review fields.

    Edge case: when ``case_type`` is ``NO_CASES`` (the agent had no cases
    this month), ``scorecard_data`` and ``final_score`` must be null —
    the review still counts towards the monthly quota of 6.
    """

    model_config = ConfigDict(extra="forbid")

    support_agent_id: int
    qa_id: int
    case_type: CaseTypeEnum
    scorecard_data: dict | None = None
    notes: str | None = None
    final_score: float | None = None

    @model_validator(mode="after")
    def validate_no_cases_has_null_scores(self) -> "ReviewBase":
        """A 'No Cases' review must not carry scores."""
        if self.case_type is CaseTypeEnum.NO_CASES:
            if self.scorecard_data is not None or self.final_score is not None:
                raise ValueError(
                    "Reviews with case_type='No Cases' must have null "
                    "scorecard_data and null final_score."
                )
        return self


class ReviewCreate(ReviewBase):
    """Payload for creating a review."""


class ReviewRead(ReviewBase):
    """Review representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
