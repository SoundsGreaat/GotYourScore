"""AI assistant Pydantic schemas."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import CaseTypeEnum


class _HtmlIn(BaseModel):
    """Shared payload field: rich-text (HTML) QA notes.

    The upper bound guards against unbounded per-request LLM cost.
    """

    html: str = Field(min_length=1, max_length=100_000)

    @field_validator("html")
    @classmethod
    def reject_blank_html(cls, value: str) -> str:
        """Whitespace-only notes carry nothing to analyze or rewrite."""
        if not value.strip():
            raise ValueError("html must not be blank.")
        return value


class RefactorIn(_HtmlIn):
    """Payload for POST /api/ai/refactor."""

    model_config = ConfigDict(extra="forbid")


class RefactorOut(BaseModel):
    """Response for POST /api/ai/refactor: the improved HTML."""

    html: str


class ScoreIn(_HtmlIn):
    """Payload for POST /api/ai/score (preview scoring, nothing saved)."""

    model_config = ConfigDict(extra="forbid")

    case_type: CaseTypeEnum

    @model_validator(mode="after")
    def reject_no_cases(self) -> "ScoreIn":
        """'No Cases' has nothing to score — same rule as auto-score."""
        if self.case_type is CaseTypeEnum.NO_CASES:
            raise ValueError(
                "case_type 'No Cases' cannot be used with AI scoring."
            )
        return self


class ScoreOut(BaseModel):
    """Response for POST /api/ai/score.

    ``final_score`` is a multiplier-free preview computed as
    ``max(0, 100 - total_deduction)``: progressive multipliers need an
    agent context and apply only when a review is saved via
    POST /api/reviews/auto-score.
    """

    scorecard: dict[str, int]
    total_deduction: int
    base_score: int
    final_score: int
