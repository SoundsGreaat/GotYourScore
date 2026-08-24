"""AI assistant Pydantic schemas."""

from typing import Annotated

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


class NotesFromScoreIn(BaseModel):
    """Payload for POST /api/ai/notes-from-score (drafting only, nothing saved).

    ``raw_scorecard`` maps error keys to the deducted points the QA
    ticked; keys unknown to the case type's active rules are skipped by
    the service. Same shape as ScorePreviewRequest: whole numbers >= 1
    (booleans are rejected by pydantic's int rules) and a size cap
    against pathological request sizes.

    ``support_agent_id`` is optional: when given, the service computes
    the agent's progressive multipliers (past occurrences of each
    repeated error) and the final score, so the drafted notes can
    justify the amplified penalties. ``exclude_review_id`` and
    ``no_multiplier_keys`` mirror ScorePreviewRequest semantics (the
    edited row's own deductions must not inflate its multipliers;
    QA-waived progressions keep multiplier 1).

    ``case_type='No Cases'`` is rejected by the ENDPOINT with 400 (not
    here) because there is no scorecard to reference — a schema-level
    validator would produce a 422 instead.
    """

    model_config = ConfigDict(extra="forbid")

    case_type: CaseTypeEnum
    raw_scorecard: dict[str, Annotated[int, Field(ge=1)]] = Field(
        max_length=60
    )
    support_agent_id: int | None = None
    exclude_review_id: int | None = None
    no_multiplier_keys: set[str] = Field(default_factory=set, max_length=60)


class NotesFromScoreOut(BaseModel):
    """Response for POST /api/ai/notes-from-score.

    ``notes_html`` is an UNsanitized-on-server HTML fragment (allowed
    tags <p>, <ul>, <ol>, <li>, <strong>, <em>, <br>); the client must
    sanitize it with DOMPurify before insertion — same trust boundary
    as POST /api/ai/refactor.
    """

    notes_html: str
