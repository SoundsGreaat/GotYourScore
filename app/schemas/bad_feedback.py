"""Bad Feedback Pydantic schemas."""

from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator

from app.models.enums import AgentKindEnum, FaultEnum, ReviewStatusEnum


TrackerFieldInput = str | StrictInt | StrictFloat | None


class BadFeedbackAgentIn(BaseModel):
    """One involved agent in an upsert payload.

    ``user_id`` refers to an existing user (import pre-creates unknown
    ones). ``fault``/``qa_comment`` ride along on create as nulls and
    are the main payload on edit.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    kind: AgentKindEnum
    fault: FaultEnum | None = None
    qa_comment: str | None = Field(default=None, max_length=10_000)


class BadFeedbackAgentRead(BaseModel):
    """One involved agent as returned by the API.

    ``user_label`` resolves server-side to ``nickname`` (or name for
    Google accounts) so the UI never needs a user lookup round-trip.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    kind: AgentKindEnum
    fault: FaultEnum | None = None
    qa_comment: str | None = None
    user_label: str | None = None


class _TrackerCreate(BaseModel):
    """Payload for creating one Bad Feedback record
    (POST /api/bad-feedback). Import passes plain field values too.

    ``status``/``qa_id`` are server-managed: created as pending;
    ``created_by`` injected from the authenticated caller. ``complete``
    short-circuits the pending stage (the New Bad Feedback modal's
    "Save"): the creator finishes the record in the same request, so
    ``qa_id`` = caller and ``completed_at`` are stamped atomically —
    the finisher-becomes-qa_id rule stays auditable. Omitting it (the
    "Save For Later" path) leaves the record pending in the queue.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=255)
    customer_info: str | None = Field(default=None, max_length=2000)
    customer_feedback: str | None = Field(default=None, max_length=10_000)
    related_case: str | None = Field(default=None, max_length=255)
    # Stable field key -> string, number (numeric fields), or null.
    # Legacy flat fields remain supported;
    # when both forms provide the same key, this mapping takes precedence.
    field_values: dict[str, TrackerFieldInput] = Field(default_factory=dict)
    assigned_qa_id: int | None = None
    agents: list[BadFeedbackAgentIn] = Field(default_factory=list, max_length=50)
    complete: bool = False

    @field_validator("source", "customer_info", "related_case")
    @classmethod
    def strip_blank_to_none(cls, value: str | None) -> str | None:
        """Spreadsheets leak whitespace; blanks carry no data."""
        if value is None:
            return value
        return value.strip() or None


class BadFeedbackCreate(_TrackerCreate):
    fb_date: date_type | None = None


class _TrackerUpdate(BaseModel):
    """Payload for editing a Bad Feedback record
    (PATCH /api/bad-feedback/{id}).

    ``agents`` is REPLACE-ALL: the client sends the full desired list
    (existing + newly added cards), the server syncs by ``(user_id,
    kind)`` and rewrites fault/comment per row. Simple and matches the
    edit modal which always renders the complete card list.

    Tri-state on ``assigned_qa_id`` via ``model_fields_set``: key
    present with null clears the assignment (back to shared queue).

    Completion is NOT part of this payload: it is an explicit action
    (POST .../complete) so the finisher-becomes-qa_id rule stays
    auditable.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=255)
    customer_info: str | None = Field(default=None, max_length=2000)
    customer_feedback: str | None = Field(default=None, max_length=10_000)
    related_case: str | None = Field(default=None, max_length=255)
    # PATCH semantics are per-key merge: omitted keys are untouched and a
    # present null clears that configured field.
    field_values: dict[str, TrackerFieldInput] = Field(default_factory=dict)
    assigned_qa_id: int | None = None
    agents: list[BadFeedbackAgentIn] | None = Field(default=None, max_length=50)

    @field_validator("source", "customer_info", "related_case")
    @classmethod
    def strip_blank_to_none(cls, value: str | None) -> str | None:
        """Spreadsheets leak whitespace; blanks carry no data."""
        if value is None:
            return value
        return value.strip() or None


class BadFeedbackUpdate(_TrackerUpdate):
    fb_date: date_type | None = None


class _TrackerRead(BaseModel):
    """Shared tracker record fields as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str | None = None
    customer_info: str | None = None
    customer_feedback: str | None = None
    related_case: str | None = None
    # Canonical values use their stable keys (dates are YYYY-MM-DD/null);
    # custom values are included, including values retained for archived keys.
    # Numeric values use strings so clients can round-trip their full precision.
    field_values: dict[str, str | None] = Field(default_factory=dict)
    qa_comment: str | None = None
    status: ReviewStatusEnum = ReviewStatusEnum.PENDING
    assigned_qa_id: int | None = None
    qa_id: int | None = None
    completed_at: datetime | None = None
    created_by: int | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    agents: list[BadFeedbackAgentRead] = Field(default_factory=list)


class BadFeedbackRead(_TrackerRead):
    fb_date: date_type | None = None


class ImportPreviewRow(BaseModel):
    """One selectable non-empty spreadsheet row shown before import."""

    row_number: int
    values: list[str]


class ImportInspectResponse(BaseModel):
    """Headers + preview returned by POST /api/bad-feedback/import/inspect.

    ``headers`` keeps the sheet's original text (mapping UI shows it
    verbatim); ``suggestions`` maps canonical field keys to the
    best-guess header text (empty when no match). ``preview`` contains
    selectable raw rows, including their original sheet row numbers, so
    the user can exclude individual records before committing.
    """

    sheet_names: list[str]
    active_sheet: str
    headers: list[str]
    # Canonical field key -> suggested header text (or absent).
    suggestions: dict[str, str]
    # Active case fields and the two agent mappings, in mapping screen order.
    field_labels: dict[str, str]
    required_fields: list[str]
    preview: list[ImportPreviewRow]
    total_rows: int


class ImportCommitRow(BaseModel):
    """One created record in the import report."""

    id: int
    related_case: str | None = None
    agent_labels: list[str] = Field(default_factory=list)


class ImportSkippedRow(BaseModel):
    """One skipped sheet row with a human-readable reason."""

    row_number: int
    reason: str
    code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    field_key: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ImportCommitResponse(BaseModel):
    """Outcome of POST /api/bad-feedback/import."""

    created: list[ImportCommitRow]
    skipped: list[ImportSkippedRow]
    created_users: list[str]
    created_count: int
    skipped_count: int


# Refund Check uses its own date key and OpenAPI schema names.
class RefundCheckAgentIn(BadFeedbackAgentIn):
    pass


class RefundCheckAgentRead(BadFeedbackAgentRead):
    pass


class RefundCheckCreate(_TrackerCreate):
    r_date: date_type | None = None
    agents: list[RefundCheckAgentIn] = Field(default_factory=list, max_length=50)


class RefundCheckUpdate(_TrackerUpdate):
    r_date: date_type | None = None
    agents: list[RefundCheckAgentIn] | None = Field(default=None, max_length=50)


class RefundCheckRead(_TrackerRead):
    r_date: date_type | None = None
    agents: list[RefundCheckAgentRead] = Field(default_factory=list)


class RefundCheckImportInspectResponse(ImportInspectResponse):
    pass


class RefundCheckImportCommitResponse(ImportCommitResponse):
    pass
