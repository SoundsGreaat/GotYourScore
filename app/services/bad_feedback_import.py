"""Smart Excel import for Bad Feedback records.

Two-phase flow keeps the upload disposable:

1. ``inspect_workbook`` — parse the uploaded xlsx in memory (nothing is
   persisted), return sheet names, the active sheet's header row, the
   first data rows as display strings and best-guess suggestions that
   map canonical field keys (``date``, ``source``, ...) to headers.
2. ``commit_import`` — the client re-uploads the SAME file with the
   user-confirmed mapping; rows become ``BadFeedback`` records
   (status=pending), unknown agent names become placeholder users
   (Support or Sales role by the mapping), and the response is a
   per-row report.

Dates arrive as real Excel dates, ISO strings or human text ("12 Mar",
"12.03") with the year often missing — missing years default to the
current one. Agent cells hold comma/semicolon/pipe/newline-separated
lists; each name is resolved by email (case-insensitive) or nickname,
unknown names are slugified (Cyrillic transliterated) into
``nickname@ALLOWED_DOMAIN`` placeholder accounts.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from datetime import date, datetime
from io import BytesIO
from typing import Any, Sequence
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._reader import FORMULA_TAG, VALUE_TAG, WorkSheetParser, _cast_number
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentKindEnum,
    BadFeedback,
    BadFeedbackAgent,
    RefundCheck,
    RefundCheckAgent,
    ReviewStatusEnum,
    RoleEnum,
    TrackerField,
    User,
)
from app.schemas.bad_feedback import (
    ImportCommitResponse,
    ImportInspectResponse,
    ImportPreviewRow,
    ImportSkippedRow,
)
from app.services import user_service
from app.services.tracker_fields import (
    NUMERIC_FIELD_TYPES,
    InvalidNumericFieldValue,
    apply_field_values,
    field_value_limit,
    validate_numeric_value,
)


class ImportValidationError(ValueError):
    """Expected client-correctable workbook or mapping validation failure."""


class WorkbookParseError(Exception):
    """Known invalid/corrupt workbook structure, distinct from application errors."""


WORKBOOK_PARSE_ERRORS = (
    BadZipFile,
    InvalidFileException,
    ParseError,
    EOFError,
)
_WORKBOOK_OPEN_ERRORS = (*WORKBOOK_PARSE_ERRORS, KeyError, OSError, ValueError)
_UNCACHED_FORMULA = object()


def _load_workbook(content: bytes):
    try:
        return load_workbook(BytesIO(content), data_only=True, read_only=True)
    except _WORKBOOK_OPEN_ERRORS as exc:
        raise WorkbookParseError from exc


class _ExactNumber(str):
    """The original Excel number, before conversion for a legacy mapping."""


class _ExactNumberParser(WorkSheetParser):
    """Keep the stored numeric token before openpyxl casts it to a float."""

    def __init__(self, source, sheet, columns: set[int] | None):
        workbook = sheet.parent
        super().__init__(
            source,
            sheet._shared_strings,
            data_only=True,
            epoch=workbook.epoch,
            date_formats=workbook._date_formats,
            timedelta_formats=workbook._timedelta_formats,
        )
        self.columns = columns

    def parse_cell(self, element):
        if self.row_counter == 1:
            return super().parse_cell(element)  # Keep header labels unchanged.
        if self.columns is not None:
            coordinate = element.get("r")
            column = coordinate_to_tuple(coordinate)[1] if coordinate else self.col_counter + 1
            if column not in self.columns:
                return super().parse_cell(element)

        value = element.findtext(VALUE_TAG)
        data_type = element.get("t", "n")
        no_saved_result = value is None or (value == "" and data_type != "str")
        if no_saved_result and element.find(FORMULA_TAG) is not None:
            cell = super().parse_cell(element)
            cell["value"] = _UNCACHED_FORMULA
            return cell
        if value and data_type == "n":
            style_id = int(element.get("s") or 0)
            if style_id not in self.date_formats:
                element.set("t", "str")
                cell = super().parse_cell(element)
                cell["value"] = _ExactNumber(value)
                return cell
        return super().parse_cell(element)


def _rows_with_exact_numbers(sheet, columns: set[int] | None = None):
    # The public iterator doesn't expose raw tokens. Reuse openpyxl's
    # read-only parser for strings, dates, and cached formulas, overriding
    # just numeric decoding within the selected columns (all for preview).
    next_row = 1
    max_row = sheet.max_row
    max_column = sheet.max_column
    empty_row = (None,) * (max_column or 0)
    with sheet._get_source() as source:
        for row_number, cells in _ExactNumberParser(source, sheet, columns).parse():
            if max_row is not None and row_number > max_row:
                break
            for _ in range(next_row, row_number):
                yield empty_row
            if row_number >= next_row:
                yield sheet._get_row(cells, 1, max_column, values_only=True)
                next_row = row_number + 1


# Canonical importable fields. ``support_agents``/``sales_agents`` are
# the two agent-list columns; everything else is a scalar field.
FIELD_KEYS = (
    "date",
    "source",
    "customer_info",
    "customer_feedback",
    "related_case",
    "support_agents",
    "sales_agents",
)

# Human labels used by the mapping UI, keyed like FIELD_KEYS.
FIELD_LABELS = {
    "date": "Date",
    "source": "Source",
    "customer_info": "Customer's info (CRM link / email)",
    "customer_feedback": "Customer's feedback",
    "related_case": "RA related case",
    "support_agents": "RA Support agents assigned",
    "sales_agents": "Sales agents assigned",
}

# Historic workbook headers remain recognized after the tracker editor
# switches to shorter display labels.
_FIELD_LABEL_ALIASES = {
    "fb_date": ("Date",),
    "r_date": ("Date",),
    "source": ("Source",),
    "customer_info": ("Customer's info (CRM link / email)",),
    "customer_feedback": ("Customer's feedback",),
    "related_case": ("RA related case",),
}

# Regexes tried in order by _parse_date_text.
_DATE_PATTERNS = (
    # 2024-03-12 / 2024.03.12 / 2024/03/12
    (re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$"), "ymd"),
    # 12.03.2024 / 12-03-2024 / 12/03/2024 (day-first per team practice)
    (re.compile(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$"), "dmy"),
    # 12.03 / 12-03 / 12/03 — no year -> current year
    (re.compile(r"^(\d{1,2})[-./](\d{1,2})$"), "dm"),
    # 12 Mar 2024 / 12 March / "12 мар"
    (re.compile(
        r"^(\d{1,2})[\s.-]*([A-Za-zА-Яа-я]{3,9})\.?(?:[\s,.-]+(\d{4}))?$"
    ), "dmon"),
    # Mar 12 / March 12, 2024 / "мар 12"
    (re.compile(
        r"^([A-Za-zА-Яа-я]{3,9})\.?(?:[\s,.-]+(\d{1,2}))?(?:[\s,.-]+(\d{4}))?$"
    ), "mond"),
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ),
        start=1,
    )
}
for _ru, _idx in (
    ("янв", 1), ("фев", 2), ("мар", 3), ("апр", 4), ("май", 5), ("июн", 6),
    ("июл", 7), ("авг", 8), ("сен", 9), ("окт", 10), ("ноя", 11), ("дек", 12),
):
    _MONTHS[_ru] = _idx

# Agent-cell separators: comma, semicolon, pipe, newline.
_AGENT_SPLIT_RE = re.compile(r"[,;|\n\r]+")

# Cyrillic -> Latin, for slugifying agent names into nicknames.
# Team practice: sheet names are plain English nicknames already, so
# this map only exists as a safety net for stray non-Latin characters.
_CYRILLIC_MAP = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d",
    "е": "e", "є": "ie", "ж": "zh", "з": "z", "и": "i", "і": "i",
    "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ы": "y", "э": "e", "ю": "iu", "я": "ia", "ё": "e",
})

# Nickname chars kept verbatim; everything else collapses to '-'.
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9._-]+")

# Safe local parts can be used directly as nicknames when the cell
# holds an email.
_NICKNAME_SAFE_RE = re.compile(r"^[a-z0-9._-]{1,50}$")

_MAX_ROWS = 1000
_MAX_CELL = 2000

# Row-level skip reasons.
REASON_NO_MAPPED_VALUES = "no usable mapped values"
REASON_BAD_DATE = "unparsable date (fix it in the sheet or clear the cell)"
REASON_FIELD_TOO_LONG = "value exceeds the field's maximum length"


def _display(value: Any) -> str | None:
    """Normalize any Excel cell to a trimmed display string or None."""
    if value is None:
        return None
    if value is _UNCACHED_FORMULA:
        return "Formula has no saved result"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _parse_date_text(text: str, today: date) -> date | None:
    """Parse a human date string; missing year -> ``today.year``.

    Day-first for numeric forms (team practice: "12.03" is March 12).
    Returns None when nothing matches — the row is skipped with a
    reason rather than silently mis-dated.
    """
    text = text.strip().strip('"').strip()
    if not text:
        return None
    lowered = text.lower().replace(",", " ")
    for pattern, shape in _DATE_PATTERNS:
        match = pattern.match(lowered)
        if not match:
            continue
        groups = match.groups()
        try:
            if shape == "ymd":
                y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
            elif shape == "dmy":
                d, m, y = int(groups[0]), int(groups[1]), int(groups[2])
            elif shape == "dm":
                d, m, y = int(groups[0]), int(groups[1]), today.year
            elif shape == "dmon":
                d = int(groups[0])
                m = _MONTHS.get(groups[1][:3])
                if m is None:
                    continue
                y = int(groups[2]) if groups[2] else today.year
            else:  # mond
                m = _MONTHS.get(groups[0][:3])
                if m is None:
                    continue
                d = int(groups[1]) if groups[1] else 1
                y = int(groups[2]) if groups[2] else today.year
        except (TypeError, ValueError):
            continue
        try:
            return date(y, m, d)
        except ValueError:
            continue
    return None


def parse_feedback_date(value: Any, today: date | None = None) -> date | None:
    """Coerce an Excel cell into a date.

    Real dates/datetimes pass through; strings go through
    :func:`_parse_date_text` (missing year -> current year). Unparsable
    text returns None.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _parse_date_text(str(value), today or date.today())


def _split_agents(raw: str) -> list[str]:
    """Split an agent cell into cleaned name tokens."""
    tokens = (t.strip(" .\u00a0\t") for t in _AGENT_SPLIT_RE.split(raw))
    return [t for t in tokens if t]


def _slugify_name(raw: str) -> str:
    """Turn an agent display name into a valid placeholder nickname.

    Transliterates Cyrillic, keeps ``[a-z0-9._-]`` (anything else ->
    '-'), collapses separators, trims to 50 chars. Returns '' when
    nothing usable survives.
    """
    text = unicodedata.normalize("NFKD", raw.strip().lower())
    translit = text.translate(_CYRILLIC_MAP)
    slug = _SLUG_KEEP_RE.sub("-", translit).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:50]


def kind_to_role(kind: AgentKindEnum) -> RoleEnum:
    """Agent kind -> the role a synthesized account receives."""
    return (
        RoleEnum.SUPPORT if kind is AgentKindEnum.SUPPORT else RoleEnum.SALES
    )


def _ensure_frontline_role(user: User, kind: AgentKindEnum) -> None:
    """Grant a found user the front-line role implied by the column.

    Front-line roles are additive badges: a QA-only account referenced
    by a sheet gains Support/Sales without losing anything, and a user
    already holding the OTHER front-line role gains this one too.
    """
    role = kind_to_role(kind)
    if role not in user.roles:
        user.roles = [*user.roles, role]


def _resolve_or_create_user(
    raw_name: str,
    kind: AgentKindEnum,
    known_by_email: dict[str, User],
    known_by_nick: dict[str, User],
    created_log: list[str],
) -> User | None:
    """Find a user by email/nickname or synthesize a placeholder.

    Lookup: full email (case-insensitive) -> nickname slug. Unknown
    names become ``{slug}@ALLOWED_DOMAIN`` accounts with the role
    implied by the column (Support/Sales).
    """
    lowered = raw_name.strip().lower()
    nick = _slugify_name(raw_name)
    user = known_by_email.get(lowered)
    if user is None and "@" in lowered:
        local = lowered.split("@", 1)[0]
        local_nick = local if _NICKNAME_SAFE_RE.fullmatch(local) else _slugify_name(local)
        user = known_by_nick.get(local_nick)
    if user is None and nick:
        user = known_by_nick.get(nick)
    if user is not None:
        _ensure_frontline_role(user, kind)
        return user

    if not nick:
        return None
    email = user_service.placeholder_email(nick)
    user = User(email=email, name=None, roles=[kind_to_role(kind)])
    created_log.append(f"{raw_name.strip()} -> {email}")
    known_by_email[email] = user
    known_by_nick.setdefault(nick, user)
    return user


async def _load_user_maps(
    db: AsyncSession,
) -> tuple[dict[str, User], dict[str, User]]:
    """Load every non-deleted user into email/nickname lookup maps."""
    rows = (await db.execute(select(User).where(User.active_filter()))).scalars()
    by_email: dict[str, User] = {}
    by_nick: dict[str, User] = {}
    for user in rows:
        by_email[user.email.lower()] = user
        by_nick[user.email.split("@", 1)[0].lower()] = user
    return by_email, by_nick


def _header_index(headers: list[str], wanted: str | None) -> int | None:
    """Map a chosen header text to its column index (None = unmapped)."""
    if not wanted:
        return None
    wanted = wanted.strip()
    for idx, header in enumerate(headers):
        if header.strip() == wanted:
            return idx
    return None


_MAPPING_THRESHOLD = 0.65
_MAPPING_AMBIGUITY_MARGIN = 0.08


def _normalized_text(value: str) -> str:
    """Lowercase text while preserving Unicode word boundaries."""
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _field_label(field: str) -> str:
    """Return the visible label without optional explanatory text."""
    return FIELD_LABELS[field].split("(", maxsplit=1)[0].strip()


def _label_similarity(label: str, header: str) -> float:
    """Return normalized similarity between an import field and a header.

    Labels are the single source of matching vocabulary. Exact and phrase
    matches win; otherwise token overlap and edit similarity handle short
    labels and small spelling differences.
    """
    label = _normalized_text(label.split("(", maxsplit=1)[0].strip())
    normalized = _normalized_text(header)
    if not label or not normalized:
        return 0.0
    if normalized == label:
        return 1.0
    if label in normalized or normalized in label:
        length_ratio = min(len(normalized), len(label)) / max(
            len(normalized), len(label)
        )
        return 0.7 + 0.3 * length_ratio
    header_tokens = set(normalized.split())
    label_tokens = set(label.split())
    token_score = (2 * len(header_tokens & label_tokens)) / (
        len(header_tokens) + len(label_tokens)
    )
    text_score = SequenceMatcher(None, normalized, label).ratio()
    return 0.6 * token_score + 0.4 * text_score


def _field_similarity(field: str, header: str) -> float:
    """Compatibility wrapper for the original Bad Feedback labels."""
    return _label_similarity(FIELD_LABELS[field], header)


def import_field_labels(fields: Sequence[TrackerField]) -> dict[str, str]:
    """Return configured case labels plus the fixed agent-mapping labels."""
    labels = {field.key: field.label for field in fields}
    labels["support_agents"] = FIELD_LABELS["support_agents"]
    labels["sales_agents"] = FIELD_LABELS["sales_agents"]
    return labels


def _required_field_labels(fields: Sequence[TrackerField]) -> dict[str, str]:
    return {field.key: field.label for field in fields if field.required_for_import}


def _suggest_mapping_for_labels(
    headers: list[str], field_labels: dict[str, str]
) -> dict[str, str]:
    """Suggest one best unambiguous header per configured import field."""
    candidates: list[tuple[float, str, int]] = []
    for index, header in enumerate(headers):
        scored = sorted(
            (
                (
                    max(
                        _label_similarity(candidate, header)
                        for candidate in (label, *_FIELD_LABEL_ALIASES.get(key, ()))
                    ),
                    key,
                )
                for key, label in field_labels.items()
            ),
            reverse=True,
        )
        if not scored:
            continue
        best_score, best_key = scored[0]
        next_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score < _MAPPING_THRESHOLD:
            continue
        if (
            next_score >= _MAPPING_THRESHOLD
            and best_score - next_score < _MAPPING_AMBIGUITY_MARGIN
        ):
            continue
        candidates.append((best_score, best_key, index))

    suggestions: dict[str, str] = {}
    used_fields: set[str] = set()
    used_headers: set[int] = set()
    for _, key, index in sorted(
        candidates, key=lambda item: (-item[0], item[1], item[2])
    ):
        if key in used_fields or index in used_headers:
            continue
        suggestions[key] = headers[index]
        used_fields.add(key)
        used_headers.add(index)
    return suggestions


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    """Map each field to its best unambiguous header by similarity."""
    return _suggest_mapping_for_labels(
        headers, {key: FIELD_LABELS[key] for key in FIELD_KEYS}
    )


def inspect_workbook(
    content: bytes,
    fields: Sequence[TrackerField] | None = None,
    *,
    include_legacy_date_alias: bool = False,
) -> ImportInspectResponse:
    """Parse the uploaded workbook in memory (nothing persisted)."""
    workbook = _load_workbook(content)
    try:
        if not workbook.sheetnames:
            raise ImportValidationError("The workbook contains no sheets.")
        sheet = workbook[workbook.sheetnames[0]]
        rows = (
            _rows_with_exact_numbers(sheet)
            if any(field.field_type in NUMERIC_FIELD_TYPES for field in fields or ())
            else sheet.iter_rows(values_only=True)
        )
        try:
            header_row = next(rows)
        except StopIteration:
            raise ImportValidationError("The first sheet is empty.")
        headers = [
            _display(value) or f"Column {idx + 1}"
            for idx, value in enumerate(header_row)
        ]
        data_rows: list[ImportPreviewRow] = []
        total = 0
        for row_number, values in enumerate(rows, start=2):
            if not any(v is not None and str(v).strip() for v in values):
                continue
            total += 1
            # Keep the physical-row limit identical to commit_import(). A
            # formatted sheet can contain blank rows, so a non-empty row
            # beyond this range must not appear selectable in the preview.
            if row_number - 2 < _MAX_ROWS:
                data_rows.append(
                    ImportPreviewRow(
                        row_number=row_number,
                        values=[_display(v) or "" for v in values[: len(headers)]],
                    )
                )
        field_labels = (
            FIELD_LABELS if fields is None else import_field_labels(fields)
        )
        suggestions = _suggest_mapping_for_labels(headers, field_labels)
        if include_legacy_date_alias and "fb_date" in suggestions:
            # Keep the old Bad Feedback suggestion without adding a second
            # Date picker to the configurable-field UI.
            suggestions["date"] = suggestions["fb_date"]
        return ImportInspectResponse(
            sheet_names=list(workbook.sheetnames),
            active_sheet=sheet.title,
            headers=headers,
            suggestions=suggestions,
            field_labels=field_labels,
            required_fields=list(_required_field_labels(fields or ())),
            preview=data_rows,
            total_rows=total,
        )
    finally:
        workbook.close()


async def commit_import(
    db: AsyncSession,
    content: bytes,
    mapping: dict[str, str | None],
    excluded_rows: set[int] | None,
    assigned_qa_id: int | None,
    created_by: int,
    *,
    tracker: str = "bad-feedback",
    fields: Sequence[TrackerField] | None = None,
    include_legacy_date_alias: bool = False,
) -> ImportCommitResponse:
    """Create pending tracker rows from the mapped sheet.

    Configured case fields use their stable keys. The two agent mappings
    remain fixed as ``support_agents`` and ``sales_agents``. For legacy
    Bad Feedback callers, ``date`` is accepted as an alias for ``fb_date``.
    """
    if tracker not in {"bad-feedback", "refund-check"}:
        raise RuntimeError("Unsupported tracker configuration.")
    record_model = BadFeedback if tracker == "bad-feedback" else RefundCheck
    agent_model = BadFeedbackAgent if tracker == "bad-feedback" else RefundCheckAgent

    if fields is None:
        # Preserve direct callers of this service's original interface.
        field_specs = [
            (key, "fb_date" if key == "date" else key, field_type)
            for key, field_type in (
                ("date", "date"),
                ("source", "text"),
                ("customer_info", "text"),
                ("customer_feedback", "textarea"),
                ("related_case", "text"),
            )
        ]
    else:
        field_specs = [
            (field.key, field.key, field.field_type) for field in fields
        ]
    field_labels = import_field_labels(fields or ())
    required_fields = _required_field_labels(fields or ())

    workbook = _load_workbook(content)
    try:
        if not workbook.sheetnames:
            raise ImportValidationError("The workbook contains no sheets.")
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            raise ImportValidationError("The first sheet is empty.")
        headers = [
            _display(value) or f"Column {idx + 1}"
            for idx, value in enumerate(header_row)
        ]
        mapping = dict(mapping)
        if include_legacy_date_alias and "fb_date" not in mapping and "date" in mapping:
            mapping["fb_date"] = mapping["date"]
        valid_keys = {
            mapping_key for mapping_key, _, _ in field_specs
        } | {"support_agents", "sales_agents"}
        if include_legacy_date_alias:
            valid_keys.add("date")
        unknown_keys = set(mapping) - valid_keys
        if unknown_keys:
            raise ImportValidationError("Import mapping contains an unknown field.")
        if any(value is not None and not isinstance(value, str) for value in mapping.values()):
            raise ImportValidationError(
                "Import mapping values must be header text or null."
            )
        columns = {
            mapping_key: _header_index(headers, mapping.get(mapping_key))
            for mapping_key, _, _ in field_specs
        }
        missing_mappings = [
            label for key, label in required_fields.items() if columns[key] is None
        ]
        if missing_mappings:
            raise ImportValidationError(
                "Map a workbook column for each required field: "
                + ", ".join(missing_mappings)
                + "."
            )
        columns["support_agents"] = _header_index(
            headers, mapping.get("support_agents")
        )
        columns["sales_agents"] = _header_index(headers, mapping.get("sales_agents"))
        numeric_fields = {
            key for key, _, field_type in field_specs if field_type in NUMERIC_FIELD_TYPES
        }
        numeric_columns = {
            column + 1 for key in numeric_fields if (column := columns[key]) is not None
        }
        if numeric_columns:
            rows.close()
            rows = _rows_with_exact_numbers(sheet, numeric_columns)
            next(rows)  # Already read the headers to resolve the mapping.

        by_email, by_nick = await _load_user_maps(db)
        today = date.today()
        created: list[BadFeedback | RefundCheck] = []
        skipped: list[ImportSkippedRow] = []
        created_users: list[str] = []

        excluded_rows = excluded_rows or set()
        for row_number, values in enumerate(rows, start=2):
            if row_number - 2 >= _MAX_ROWS:
                skipped.append(ImportSkippedRow(row_number=row_number, reason="row limit reached"))
                continue
            if not any(v is not None and str(v).strip() for v in values):
                continue
            if row_number in excluded_rows:
                skipped.append(ImportSkippedRow(row_number=row_number, reason="excluded in preview"))
                continue

            def cell(key: str) -> Any:
                idx = columns.get(key)
                if idx is None or idx >= len(values):
                    return None
                value = values[idx]
                if key not in numeric_fields:
                    # A header may be mapped to more than one field. Keep
                    # the old interpretation for any non-numeric mapping.
                    if value is _UNCACHED_FORMULA:
                        return None
                    if isinstance(value, _ExactNumber):
                        try:
                            return _cast_number(value)
                        except ValueError:
                            # The numeric mapping will reject this row.
                            return value
                return value

            def text(key: str) -> str | None:
                value = _display(cell(key))
                if value is not None and len(value) > _MAX_CELL:
                    value = value[:_MAX_CELL]
                return value

            missing_values = [
                label for key, label in required_fields.items()
                if _display(cell(key)) is None
            ]
            if missing_values:
                skipped.append(
                    ImportSkippedRow(
                        row_number=row_number,
                        reason="Missing required values: " + ", ".join(missing_values),
                    )
                )
                continue

            case_values: dict[str, str | None] = {}
            row_error: ImportSkippedRow | None = None
            for mapping_key, field_key, field_type in field_specs:
                if columns[mapping_key] is None:
                    continue
                raw_value = cell(mapping_key)
                if field_type in NUMERIC_FIELD_TYPES:
                    try:
                        number = validate_numeric_value(field_key, field_type, raw_value)
                    except InvalidNumericFieldValue as exc:
                        row_error = ImportSkippedRow(
                            row_number=row_number,
                            reason=f"{field_labels[field_key]}: {exc}",
                            code=exc.code,
                            field_key=exc.field_key,
                        )
                        break
                    if number is not None:
                        case_values[field_key] = number
                    continue

                value = _display(raw_value)
                if field_type == "date":
                    parsed_date = parse_feedback_date(raw_value, today)
                    if value is not None and parsed_date is None:
                        row_error = ImportSkippedRow(row_number=row_number, reason=REASON_BAD_DATE)
                        break
                    case_values[field_key] = (
                        parsed_date.isoformat() if parsed_date is not None else None
                    )
                    continue

                value_limit = field_value_limit(field_key)
                if value is not None and len(value) > value_limit:
                    row_error = ImportSkippedRow(
                        row_number=row_number,
                        reason=f"{REASON_FIELD_TOO_LONG}: {field_key} (maximum {value_limit} characters)",
                    )
                    break
                case_values[field_key] = value

            if row_error is not None:
                skipped.append(row_error)
                continue

            agent_rows: list[tuple[User, AgentKindEnum]] = []
            for key, kind in (
                ("support_agents", AgentKindEnum.SUPPORT),
                ("sales_agents", AgentKindEnum.SALES),
            ):
                raw = text(key)
                if not raw:
                    continue
                for name in _split_agents(raw):
                    user = _resolve_or_create_user(
                        name, kind, by_email, by_nick, created_users
                    )
                    if user is not None:
                        agent_rows.append((user, kind))
            if not agent_rows and not any(case_values.values()):
                skipped.append(
                    ImportSkippedRow(row_number=row_number, reason=REASON_NO_MAPPED_VALUES)
                )
                continue

            feedback = record_model(
                status=ReviewStatusEnum.PENDING,
                assigned_qa_id=assigned_qa_id,
                created_by=created_by,
                agents=[],  # Keep the collection readable after commit even when empty.
            )
            # Uses replacement semantics for JSONB and leaves compatibility
            # columns authoritative for the five seeded keys.
            apply_field_values(feedback, case_values)
            # Dedup by email, not by user.id: rows created in this
            # import have no id until the flush, so two brand-new
            # agents would collapse into one "None" key.
            seen: set[tuple[str, AgentKindEnum]] = set()
            for user, kind in agent_rows:
                pair = (user.email.lower(), kind)
                if pair in seen:
                    continue
                seen.add(pair)
                feedback.agents.append(agent_model(user=user, kind=kind))
            created.append(feedback)
            db.add(feedback)

        await db.commit()
        return ImportCommitResponse(
            created=[
                {
                    "id": fb.id,
                    "related_case": fb.related_case,
                    "agent_labels": [a.user.nickname for a in fb.agents],
                }
                for fb in created
            ],
            skipped=skipped,
            created_users=created_users,
            created_count=len(created),
            skipped_count=len(skipped),
        )
    finally:
        workbook.close()
