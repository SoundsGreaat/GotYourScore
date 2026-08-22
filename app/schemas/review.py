"""Review Pydantic schemas."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Optional reference to the reviewed case in the ticketing system.
    case_number: str | None = Field(default=None, max_length=255)
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


class AutoScoreCreate(BaseModel):
    """Payload for AI auto-scoring (POST /api/reviews/auto-score).

    The caller submits a raw ticket transcript; the AI model derives
    the raw scorecard (see ``app.services.ai_service``), which then
    flows through the same progressive multiplier and monthly quota
    rules as a manual review. ``qa_id`` is injected server-side from
    the authenticated caller, exactly like ``ReviewCreate``.

    ``case_type`` classifies the reviewed ticket: ``reviews.case_type``
    is NOT NULL and the auto-score flow receives no explicit case type,
    so it defaults to SERVICE_REQUEST and callers may override it
    per request.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: int
    # Upper bound guards against unbounded per-request LLM cost.
    transcript: str = Field(min_length=1, max_length=50_000)
    case_type: CaseTypeEnum = CaseTypeEnum.SERVICE_REQUEST
    # Optional reference to the reviewed case in the ticketing system.
    case_number: str | None = Field(default=None, max_length=255)

    @field_validator("transcript")
    @classmethod
    def reject_blank_transcript(cls, value: str) -> str:
        """Whitespace-only transcripts carry nothing to score."""
        if not value.strip():
            raise ValueError("transcript must not be blank.")
        return value

    @model_validator(mode="after")
    def reject_no_cases(self) -> "AutoScoreCreate":
        """'No Cases' reviews have null scores by definition and are
        submitted manually — an auto-scored review always analyzes a
        real transcript, so NO_CASES is rejected here.
        """
        if self.case_type is CaseTypeEnum.NO_CASES:
            raise ValueError(
                "case_type 'No Cases' cannot be used with auto-scoring."
            )
        return self


class ScorePreviewRequest(BaseModel):
    """Payload for the read-only score preview
    (POST /api/reviews/score-preview).

    Same inputs a saved review would carry, but nothing is persisted:
    the response shows exactly what POST /api/reviews would compute for
    this raw scorecard (progressive multiplier included).

    ``case_type`` is a plain string here so an unknown value yields a
    400 from the handler (a ``CaseTypeEnum`` field would 422 first);
    deductions must be >= 1 because a preview only makes sense for
    entries that will actually be penalized.
    """

    model_config = ConfigDict(extra="forbid")

    support_agent_id: int
    case_type: str
    # Upper bound guards against pathological request sizes; values are
    # whole numbers >= 1 (booleans are rejected by pydantic's int rules).
    raw_scorecard: dict[str, Annotated[int, Field(ge=1)]] = Field(
        max_length=60
    )


class ScorePreviewResponse(BaseModel):
    """Multiplier-applied preview returned by
    POST /api/reviews/score-preview (identical math to Save)."""

    breakdown: dict[str, dict[str, int]]
    total_penalty: int
    final_score: int


class ReviewRead(BaseModel):
    """Review representation returned by the API.

    ``scorecard_data`` is the JSON computed server-side at save time
    and never rewritten afterwards (historical immutability). New rows
    use the nested shape::

        {
          "rules_snapshot": {
            "case_type": "Service Request",
            "template_ids": [1],
            "items": [{"error_name": ..., "display_name": ...,
                       "penalty_points": N}, ...]
          },
          "base_score": 100,
          "total_penalty": N,
          "final_score": N,
          "breakdown": {"<error_key>": {"deducted": X, "multiplier": Y,
                                        "final_penalty": Z}}
        }

    Legacy rows store only the flat breakdown at the top level. Null
    for 'No Cases' reviews.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    support_agent_id: int
    qa_id: int
    case_type: CaseTypeEnum
    case_number: str | None = None
    scorecard_data: dict[str, Any] | None = None
    notes: str | None = None
    final_score: int | None = None
    created_at: datetime


class QuotaRead(BaseModel):
    """Reporting-period review quota status for a support agent."""

    completed: int
    target: int


class IntervalComplianceRead(BaseModel):
    """One pacing interval of a QA's quota compliance report."""

    label: str
    starts_at: datetime
    ends_at: datetime
    required: int
    completed: int
    deficit: int


class QuotaComplianceRead(BaseModel):
    """Per-interval quota compliance for one QA over a reporting period.

    ``required`` per interval equals the number of assigned agents
    (each agent owes one review per interval from this QA);
    ``deficit`` is ``max(0, required - completed)``.
    """

    qa_id: int
    closing_year: int
    closing_month: int
    period_label: str
    assigned_agent_count: int
    intervals: list[IntervalComplianceRead]
    total_required: int
    total_completed: int
    total_deficit: int
