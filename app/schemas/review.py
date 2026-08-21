"""Review Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.models.enums import CaseTypeEnum


class ReviewCreate(BaseModel):
    """Payload for creating a review (POST /api/reviews).

    ``qa_id`` is intentionally NOT part of the payload: it is injected
    server-side from the authenticated QA/Supervisor/Admin user.

    ``raw_scorecard`` maps error keys to raw deduction points, e.g.
    ``{"late_response": 5}``. Deductions are whole numbers only
    (fractional values are rejected). The progressive multiplier is
    applied server-side (see ``app.services.multiplier_service``); the
    response contains the detailed scorecard instead of this raw input.
    """

    model_config = ConfigDict(extra="forbid")

    support_agent_id: int
    case_type: CaseTypeEnum
    notes: str | None = None
    raw_scorecard: dict[str, int] | None = None

    @field_validator("raw_scorecard", mode="before")
    @classmethod
    def reject_bool_deductions(cls, value: object) -> object:
        """Booleans are not valid deduction points (``true`` != 1)."""
        if isinstance(value, dict):
            for error_key, deduction in value.items():
                if isinstance(deduction, bool):
                    raise ValueError(
                        f"Deduction for error key '{error_key}' must be a "
                        f"whole number, not a boolean."
                    )
        return value

    @field_validator("raw_scorecard")
    @classmethod
    def reject_negative_deductions(
        cls, value: dict[str, int] | None
    ) -> dict[str, int] | None:
        """Deductions must be >= 0 (0 means "no error")."""
        if value is None:
            return value
        for error_key, deduction in value.items():
            if deduction < 0:
                raise ValueError(
                    f"Deduction for error key '{error_key}' must be >= 0 "
                    f"(got {deduction})."
                )
        return value

    @model_validator(mode="after")
    def validate_no_cases_has_empty_scorecard(self) -> "ReviewCreate":
        """A 'No Cases' review must not carry any scorecard data."""
        if self.case_type is CaseTypeEnum.NO_CASES and self.raw_scorecard:
            raise ValueError(
                "Reviews with case_type='No Cases' must have a null or empty "
                "raw_scorecard."
            )
        return self


class ReviewRead(BaseModel):
    """Review representation returned by the API.

    ``scorecard_data`` is the detailed JSON computed server-side:
    ``{"<error_key>": {"deducted": X, "multiplier": Y, "final_penalty": Z}}``
    with whole-number values (null for 'No Cases' reviews).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    support_agent_id: int
    qa_id: int
    case_type: CaseTypeEnum
    scorecard_data: dict[str, dict[str, int]] | None = None
    notes: str | None = None
    final_score: int | None = None
    created_at: datetime


class QuotaRead(BaseModel):
    """Monthly review quota status for a support agent."""

    completed: int
    target: int
