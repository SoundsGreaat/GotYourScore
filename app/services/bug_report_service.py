"""Privacy-preserving preparation of public bug reports."""

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.bug_reports import BugReportInput
from app.services.ai_service import (
    BugReportPrivacyError,
    screen_bug_report_privacy,
)

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>()\[\]{}]+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|mil|int|io|dev|app|co|"
    r"uk|ca|au|nz|de|fr|jp|us|info|biz|me|cloud|ai|tech|xyz)"
    r"(?![\w.-])"
    r"(?:/[^\s<>()\[\]{}]*)?"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)
_TOKEN = re.compile(
    r"(?i)\b(?:"
    r"bearer\s+(?:token\s+)?[\"']?[\w./+=-]{8,}"
    r"|authorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?[\"']?[^\s,;\"']+"
    r"|(?:password|passwd)\s*[:=]\s*(?:\"[^\"]{1,200}\"|'[^']{1,200}'|"
    r"[^\s,;\"']+)"
    r"|(?:token|api[_ -]?key|access[_ -]?token|refresh[_ -]?token)"
    r"\s*[:=]\s*[\"']?[\w./+=-]{6,}"
    r"|(?:sk|pk|ghp|gho|github_pat|glpat|xox[baprs])[_-][A-Za-z0-9_-]{12,}"
    r")"
)
_REDACTED = "[REDACTED]"
_REDACTION_MARKER = re.compile(r"\[REDACTED\]", re.IGNORECASE)
_INVISIBLE_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u061c\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069]")


class UnsuitableBugReportError(Exception):
    """Required report fields need more detail after privacy redaction."""

    def __init__(self, fields: list[str]):
        super().__init__("Required bug report fields need more detail.")
        self.fields = fields


class BugReportPreparationError(Exception):
    """A report could not be safely prepared."""


@dataclass(frozen=True)
class PreparedReport:
    title: str
    body: str


def _deterministic_redact(value: str | None) -> str | None:
    if value is None:
        return None
    value = _INVISIBLE_CONTROLS.sub("", value)
    value = _PRIVATE_KEY.sub(_REDACTED, value)
    value = _TOKEN.sub(_REDACTED, value)
    value = _EMAIL.sub(_REDACTED, value)
    return _URL.sub(_REDACTED, value)


def _apply_spans(fields: dict[str, str], spans: list[dict[str, str]]) -> None:
    # Union exact occurrence intervals before replacement. Sequential string
    # replacement leaks the non-overlapping tails of overlapping spans.
    for field, original_value in fields.items():
        value = original_value
        intervals: list[tuple[int, int]] = []
        for span in {item["text"] for item in spans if item["field"] == field}:
            start = 0
            while (index := value.find(span, start)) >= 0:
                intervals.append((index, index + len(span)))
                start = index + 1
        intervals.sort()
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in reversed(merged):
            value = value[:start] + _REDACTED + value[end:]
        fields[field] = value


def _has_substance(value: str) -> bool:
    remainder = _REDACTION_MARKER.sub("", value)
    return any(character.isalnum() for character in remainder)


def _format_untrusted_text(value: str) -> str:
    """Render text-only Markdown without HTML, mentions, or link syntax."""
    # Escape markup delimiters and Markdown syntax; newline layout is retained.
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("@", "@\u200b")
    value = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)
    return value


def _format_plain_title(value: str) -> str:
    normalized = " ".join(value.split())
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    )


async def prepare_report(
    db: AsyncSession, report: BugReportInput
) -> PreparedReport:
    """Redact a validated report and render a server-authored Markdown body."""
    fields = {
        name: value
        for name, value in report.model_dump(exclude_none=True).items()
    }
    for name, value in fields.items():
        fields[name] = _deterministic_redact(value) or ""

    try:
        screened = await screen_bug_report_privacy(fields, db)
        spans = screened["redactions"]
        unclear = screened["unclear_fields"]
        _apply_spans(fields, spans)
    except BugReportPrivacyError:
        raise BugReportPreparationError(
            "Privacy screening is unavailable."
        ) from None
    except Exception:  # noqa: BLE001 - normalize all screening failures safely
        raise BugReportPreparationError("Privacy screening failed.") from None

    unsuitable = sorted(
        field
        for field in ("title", "steps", "actual", "expected")
        if field in unclear or not _has_substance(fields[field])
    )
    if unsuitable:
        raise UnsuitableBugReportError(unsuitable)

    title = _format_plain_title(fields["title"])
    sections = [
        ("Problem", fields["actual"]),
        ("Reproduce", fields["steps"]),
        ("Expected behavior", fields["expected"]),
    ]
    for heading, key in (
        ("Environment", "environment"),
        ("Additional context", "context"),
    ):
        if fields.get(key, "").strip():
            sections.append((heading, fields[key]))

    body = "\n\n".join(
        f"## {heading}\n\n{_format_untrusted_text(value)}"
        for heading, value in sections
    )
    body += "\n\n---\n\nSubmitted via the GotYourScore website."
    return PreparedReport(title=title, body=body)
