"""Shared tracker field configuration and value validation."""

import re
import unicodedata
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BadFeedback, RefundCheck, TrackerField

TRACKERS = frozenset({"bad-feedback", "refund-check"})
FIELD_TYPES = frozenset({"date", "text", "textarea"})
DATE_FIELD_KEYS = {"bad-feedback": "fb_date", "refund-check": "r_date"}
_SHARED_FIELD_KEYS = frozenset({"customer_info", "source", "customer_feedback", "related_case"})
MAX_FIELD_LABEL_LENGTH = 200
MAX_FIELD_VALUE_LENGTH = 10_000
MAX_FIELD_POSITION = 100_000
_CANONICAL_FIELD_MAX_LENGTHS = {
    **dict.fromkeys(DATE_FIELD_KEYS.values(), 10),
    "source": 255,
    "related_case": 255,
    "customer_info": 2_000,
    "customer_feedback": 10_000,
}

_RECORD_MODELS = {
    "bad-feedback": BadFeedback,
    "refund-check": RefundCheck,
}
# A fixed PostgreSQL advisory-lock namespace and separate key per tracker.
_FIELD_LOCK_NAMESPACE = 0x47595346  # GYSF
_FIELD_LOCK_KEYS = {"bad-feedback": 1, "refund-check": 2}
_CUSTOM_KEY_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def canonical_field_keys(tracker: str) -> frozenset[str]:
    return _SHARED_FIELD_KEYS | {DATE_FIELD_KEYS[tracker]}


def _tracker_name(tracker: str) -> str:
    if not isinstance(tracker, str) or tracker not in TRACKERS:
        raise ValueError(f"Unsupported tracker {tracker!r}.")
    return tracker


def _validate_label(label: str) -> str:
    if not isinstance(label, str):
        raise ValueError("Field label must be text.")
    label = label.strip()
    if not label:
        raise ValueError("Field label cannot be blank.")
    if len(label) > MAX_FIELD_LABEL_LENGTH:
        raise ValueError(
            f"Field label must be at most {MAX_FIELD_LABEL_LENGTH} characters."
        )
    return label


def _validate_type(field_type: str) -> str:
    if not isinstance(field_type, str) or field_type not in FIELD_TYPES:
        raise ValueError("Field type must be one of: date, text, textarea.")
    return field_type


def _validate_position(position: int) -> int:
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or position < 0
        or position > MAX_FIELD_POSITION
    ):
        raise ValueError(
            f"Field position must be an integer from 0 to {MAX_FIELD_POSITION}."
        )
    return position


def _parse_iso_date(value: str) -> date:
    if not _ISO_DATE_RE.fullmatch(value):
        raise ValueError("Date field values must use YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Date field values must be valid calendar dates.") from exc


def field_value_limit(key: str) -> int:
    """Return the shared API/import character limit for a case field."""
    return min(
        MAX_FIELD_VALUE_LENGTH,
        _CANONICAL_FIELD_MAX_LENGTHS.get(key, MAX_FIELD_VALUE_LENGTH),
    )


def _new_key(label: str) -> str:
    """Generate a stable, non-reusable machine key for a new field."""
    normalized = unicodedata.normalize("NFKD", label)
    ascii_label = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _CUSTOM_KEY_SLUG_RE.sub("_", ascii_label).strip("_") or "field"
    # Reserve suffix space within the model's 80-character key limit.
    slug = slug[:58].rstrip("_") or "field"
    return f"custom_{slug}_{uuid4().hex[:12]}"


async def list_fields(
    db: AsyncSession, tracker: str, *, include_archived: bool = False
) -> list[TrackerField]:
    """List one tracker's fields in display order."""
    tracker = _tracker_name(tracker)
    stmt = select(TrackerField).where(TrackerField.tracker == tracker)
    if not include_archived:
        stmt = stmt.where(TrackerField.archived_at.is_(None))
    stmt = stmt.order_by(TrackerField.position, TrackerField.id)
    return list((await db.execute(stmt)).scalars().all())


async def lock_field_configuration(
    db: AsyncSession, tracker: str, *, shared: bool = False
) -> None:
    """Keep field definitions stable through a record write or admin edit."""
    key = _FIELD_LOCK_KEYS[_tracker_name(tracker)]
    lock = func.pg_advisory_xact_lock_shared if shared else func.pg_advisory_xact_lock
    await db.execute(select(lock(_FIELD_LOCK_NAMESPACE, key)))


def _reorder_fields(
    fields: list[TrackerField], field: TrackerField | None = None, position: int = 0
) -> None:
    """Keep active positions dense; insertion at an occupied position shifts later fields."""
    if field is not None:
        fields.insert(position, field)
    for index, item in enumerate(fields):
        item.position = index


async def create_field(
    db: AsyncSession,
    tracker: str,
    *,
    label: str,
    field_type: str,
    position: int | None = None,
    show_in_my_reviews: bool = False,
    required_for_import: bool = False,
) -> TrackerField:
    """Create a field with a generated stable key; caller commits."""
    tracker = _tracker_name(tracker)
    label = _validate_label(label)
    if position is not None:
        position = _validate_position(position)
    field_type = _validate_type(field_type)
    await lock_field_configuration(db, tracker)
    fields = await list_fields(db, tracker)
    if len(fields) > MAX_FIELD_POSITION:
        raise ValueError("The tracker has reached its active field limit.")
    field = TrackerField(
        tracker=tracker,
        key=_new_key(label),
        label=label,
        field_type=field_type,
        show_in_my_reviews=show_in_my_reviews,
        required_for_import=required_for_import,
    )
    _reorder_fields(fields, field, len(fields) if position is None else position)
    db.add(field)
    await db.flush()
    return field


async def _field_or_error(
    db: AsyncSession, tracker: str, field_id: int
) -> TrackerField:
    field = await db.scalar(
        select(TrackerField)
        .where(TrackerField.id == field_id)
        .execution_options(populate_existing=True)
    )
    if field is None or field.tracker != tracker:
        raise ValueError("Tracker field was not found.")
    return field


async def _ensure_existing_values_fit_date(
    db: AsyncSession, tracker: str, key: str
) -> None:
    """Reject a text-to-date conversion if existing values are incompatible."""
    model = _RECORD_MODELS[tracker]
    column = (
        getattr(model, key) if key in canonical_field_keys(tracker)
        else model.field_values_data[key].astext
    )
    result = await db.execute(select(column).where(column.is_not(None)))
    for value in result.scalars():
        if not isinstance(value, str):
            raise ValueError(
                "This field cannot be changed to a date because it has "
                "non-text values."
            )
        try:
            _parse_iso_date(value)
        except ValueError as exc:
            raise ValueError(
                "This field cannot be changed to a date while it contains "
                "values that are not valid YYYY-MM-DD dates. Clear or correct "
                "those values first."
            ) from exc


async def update_field(
    db: AsyncSession,
    tracker: str,
    field_id: int,
    *,
    label: str,
    field_type: str,
    position: int | None = None,
    show_in_my_reviews: bool | None = None,
    required_for_import: bool | None = None,
) -> TrackerField:
    """Update a field's label, type and order; caller commits.

    Text and textarea are interchangeable. A change to ``date`` is
    allowed only when every stored non-null value is a valid ISO
    calendar date. This prevents a configuration edit from making
    existing values uninterpretable.
    """
    tracker = _tracker_name(tracker)
    new_label = _validate_label(label)
    new_type = _validate_type(field_type)
    new_position = (
        _validate_position(position) if position is not None else None
    )
    await lock_field_configuration(db, tracker)
    field = await _field_or_error(db, tracker, field_id)
    if field.archived_at is not None:
        raise ValueError("Restore the archived field before changing it.")
    if field.key == DATE_FIELD_KEYS[tracker] and new_type != "date":
        raise ValueError(
            "The built-in Date field must remain a date."
        )
    if new_type == "date" and field.field_type != "date":
        await _ensure_existing_values_fit_date(db, tracker, field.key)
    field.label = new_label
    field.field_type = new_type
    if show_in_my_reviews is not None:
        field.show_in_my_reviews = show_in_my_reviews
    if required_for_import is not None:
        field.required_for_import = required_for_import
    if new_position is not None:
        fields = await list_fields(db, tracker)
        fields.remove(field)
        _reorder_fields(fields, field, new_position)
    return field


async def reorder_fields(
    db: AsyncSession, tracker: str, field_ids: list[int]
) -> None:
    """Atomically set the active order, rejecting stale or foreign ID lists."""
    tracker = _tracker_name(tracker)
    if (
        not isinstance(field_ids, list)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in field_ids)
        or len(field_ids) != len(set(field_ids))
    ):
        raise ValueError("Field order must contain unique integer field IDs.")
    await lock_field_configuration(db, tracker)
    fields = await list_fields(db, tracker)
    active_ids = {field.id for field in fields}
    if set(field_ids) != active_ids:
        raise ValueError(
            "The field list is out of date. Refresh the tracker fields and try again."
        )
    by_id = {field.id: field for field in fields}
    for position, field_id in enumerate(field_ids):
        by_id[field_id].position = position


async def archive_field(
    db: AsyncSession, tracker: str, field_id: int
) -> TrackerField:
    """Hide a field without deleting its metadata or case values."""
    tracker = _tracker_name(tracker)
    await lock_field_configuration(db, tracker)
    field = await _field_or_error(db, tracker, field_id)
    if field.archived_at is None:
        fields = await list_fields(db, tracker)
        fields.remove(field)
        field.archived_at = datetime.now(timezone.utc)
        _reorder_fields(fields)
    return field


async def restore_field(
    db: AsyncSession, tracker: str, field_id: int
) -> TrackerField:
    """Restore a field under its original key; caller commits."""
    tracker = _tracker_name(tracker)
    await lock_field_configuration(db, tracker)
    field = await _field_or_error(db, tracker, field_id)
    if field.archived_at is not None:
        fields = await list_fields(db, tracker)
        if len(fields) > MAX_FIELD_POSITION:
            raise ValueError("The tracker has reached its active field limit.")
        field.archived_at = None
        _reorder_fields(fields, field, field.position)
    return field


async def validate_field_values(
    db: AsyncSession, tracker: str, values: dict[str, str | None]
) -> dict[str, str | None]:
    """Validate incoming case values against the tracker's active fields.

    Archived and unknown keys are intentionally rejected for writes; their
    old values remain available through the record's ``field_values``
    accessor and can be restored by reactivating the field.
    """
    tracker = _tracker_name(tracker)
    if not isinstance(values, dict):
        raise ValueError("field_values must be a JSON object.")
    await lock_field_configuration(db, tracker, shared=True)
    fields = {field.key: field for field in await list_fields(db, tracker)}
    if any(not isinstance(key, str) for key in values):
        raise ValueError("Field value keys must be strings.")
    unknown = set(values) - fields.keys()
    if unknown:
        key = sorted(unknown)[0]
        raise ValueError(f"Field {key!r} is not an active field for this tracker.")

    validated: dict[str, str | None] = {}
    for key, value in values.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Value for field {key!r} must be text or null.")
        max_length = field_value_limit(key)
        if value is not None and len(value) > max_length:
            raise ValueError(
                f"Value for field {key!r} exceeds {max_length} characters."
            )
        if value is not None and fields[key].field_type == "date":
            _parse_iso_date(value)
        validated[key] = value
    return validated


def apply_field_values(
    record: BadFeedback | RefundCheck, values: dict[str, str | None]
) -> None:
    """Apply already validated values to legacy columns or custom JSONB."""
    custom = dict(record.field_values_data or {})
    for key, value in values.items():
        if key in DATE_FIELD_KEYS.values():
            setattr(record, key, _parse_iso_date(value) if value is not None else None)
        elif key in _SHARED_FIELD_KEYS:
            setattr(record, key, value)
        else:
            # Replacement, rather than in-place JSONB mutation, ensures
            # SQLAlchemy detects the write and preserves siblings.
            custom[key] = value
    record.field_values_data = custom
