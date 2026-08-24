"""AI services (OpenRouter via the official OpenAI SDK).

Two capabilities:
- ``analyze_support_ticket``: score rich-text (HTML) QA notes against
  the scorecard rules configured for the case type (ScorecardTemplate
  / ScorecardItem), falling back to a generic error list when no
  rules are configured;
- ``refactor_qa_notes``: rewrite QA notes for clarity, grammar and
  professional tone while preserving the HTML markup and embedded
  images;
- ``draft_notes_from_score``: draft the review notes (a sanitized HTML
  fragment) that justify an already-ticked raw scorecard, using the
  active rules of the case type to render human-readable deductions.

OpenRouter exposes an OpenAI-compatible API, so the official ``openai``
package is used with a ``base_url`` override. The client is created
lazily and cached — mirroring ``app.core.security.get_oauth`` — so
importing this module never crashes when ``OPENROUTER_API_KEY`` is
unset.

Provider routing:
- prefer the lowest-latency OpenRouter provider;
- keep automatic fallback providers enabled.

Error contract for callers (see the reviews/ai endpoints):
- ``ValueError``: the API key is not configured -> HTTP 503.
- ``AnalyzeError``: the API call failed or the model response could
  not be parsed -> HTTP 502.

JSON extraction and scorecard sanitization are implemented as small
pure helpers (``_extract_json`` / ``_sanitize_scorecard``) so they can
be unit-tested without any network access.
"""

import json
import logging
import math
import re
from functools import lru_cache
from typing import Sequence

from openai import APIError, AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import CaseTypeEnum, ScorecardItem, ScorecardTemplate, SystemPrompt
from app.services import multiplier_service
from app.services.scorecard_service import get_active_rules


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenRouter configuration
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Exact model identifier required by the spec — do not rename.
AI_SCORING_MODEL = "deepseek/deepseek-v4-flash-0731"

# SystemPrompt key whose newest active row replaces the hardcoded
# scoring system prompt (see analyze_support_ticket).
SCORING_PROMPT_KEY = "ai_scoring"

# SystemPrompt key whose newest active row replaces the hardcoded
# refactor system prompt (see refactor_qa_notes).
REFACTOR_PROMPT_KEY = "notes_refactor"

# SystemPrompt key whose newest active row replaces the hardcoded
# notes-from-score system prompt (see draft_notes_from_score).
NOTES_FROM_SCORE_PROMPT_KEY = "notes_from_score"

# OpenRouter provider routing:
# - "latency" = prefer the provider with the lowest latency;
# - allow_fallbacks = if the preferred provider fails/unavailable,
#   OpenRouter may try another provider.
OPENROUTER_PROVIDER = {
    "sort": "latency",
    "allow_fallbacks": True,
}


# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------

# Fail fast instead of pinning a worker on a hanging LLM call.
REQUEST_TIMEOUT_SECONDS = 60.0

# OpenAI SDK retries transient failures automatically.
MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Sanitizer bounds
# ---------------------------------------------------------------------------

MAX_DEDUCTION = 100
MAX_KEY_LENGTH = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(
    r"^```[ \t]*[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```$",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a QA scoring assistant for a customer support team.

You will receive the QA notes (rich text/HTML) about a support agent's
ticket, or the ticket transcript itself. Analyze the
agent's performance and identify quality errors such as:
- late_response: the agent replied too slowly or missed the response-time
  target;
- missing_greeting: no polite greeting or introduction;
- unresolved_issue: the customer's issue was left unresolved or unconfirmed;
- wrong_information: incorrect or misleading information was given;
- poor_tone: rude, dismissive, or unprofessional tone.

Think step-by-step, carefully and in depth, about what the customer needed,
what the agent actually did, and which quality criteria were violated,
BEFORE producing the final answer. Keep that reasoning internal: it must
NOT appear in the output.

The final output must be ONLY a JSON object mapping snake_case error names
to integer deduction points, e.g. {"late_response": 5, "poor_tone": 3}:
- Keys are snake_case error names; use the names above when they fit,
  otherwise invent a descriptive snake_case name.
- Values are whole numbers between 0 and 100 (inclusive): the points
  to deduct for that error; the total scorecard starts at 100 points.
- Use 0 for a criterion you considered but found not to be violated.
- If the agent performed well, output an empty object: {}.
- Markdown code fences, prose, explanations, and nested structures are all
  forbidden: output a single flat JSON object and nothing else.
"""


REFACTOR_SYSTEM_PROMPT = """\
You are a QA writing assistant.

You will receive the HTML-formatted QA notes written by a quality
analyst about a support agent's case. Rewrite them to improve clarity,
grammar, and professional tone.

Strict rules:
- PRESERVE the HTML structure: keep every tag, attribute, and
  formatting element exactly as provided (headings, paragraphs, lists,
  tables, bold/italic, links, ...).
- PRESERVE all embedded images: keep every <img> tag with its src (and
  all other attributes) untouched.
- Do not add, remove, or reorder content beyond what clarity and
  grammar fixes require; do not translate; do not change facts, names,
  numbers, or scores.
- Output ONLY the improved HTML — no markdown code fences, no
  commentary, no explanations.
"""


NOTES_FROM_SCORE_SYSTEM_PROMPT = """\
You are a support QA reviewer writing the official review notes for an
internal quality-assurance record.

You will receive the case type, the scorecard rules that were DEDUCTED
(each as its human-readable rule name, its category, and the deducted
points), and how many rules stayed clean. A deducted rule may also show
a progressive multiplier annotation ("−5 ×2 → −10"): the agent repeated
this mistake in recent cases, so the penalty is amplified — reflect
that repetition naturally in the notes (e.g. "recurring issue"). When a
final score after multipliers is provided, the notes must be consistent
with it. Write the review NOTES that justify the resulting score: the
notes must read as if a professional QA analyst had written them by
hand about the agent's case.

Strict output rules:
- Reply with ONLY a sanitized HTML fragment. Allowed tags: <p>, <ul>,
  <ol>, <li>, <strong>, <em>, <br>. No markdown, no code fences, no
  headings, no tables, no tag attributes.
- Write 2-4 short paragraphs.
- Mention every deduction naturally in prose: group related rules into
  the same sentence or paragraph instead of listing one rule per line,
  unless a standalone deduction genuinely deserves its own paragraph.
- Never invent violations, details, names, or numbers that were not
  provided.
- When nothing was deducted, write a brief positive all-clear note
  referencing the clean review.
- Keep a neutral, factual tone suitable for an internal QA record: no
  greetings, no signatures, no filler.
"""


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

# Shared JSON-output-contract suffix appended after the configured rule
# lines. ``{penalty_map}`` is interpolated per request; ``{{}}`` renders
# the literal empty-object example.
_JSON_OUTPUT_CONTRACT = (
    "\n"
    "The final output must be ONLY a JSON object mapping snake_case error\n"
    'names to integer deduction points, e.g. {{"late_response": 5}}:\n'
    "- Keys are restricted to the error names listed above: unknown keys\n"
    "  are forbidden.\n"
    "- Values MUST match the configured penalty points exactly\n"
    "  ({penalty_map}); do not invent or adjust deductions.\n"
    "- Use 0 for a rule you considered but found not to be violated.\n"
    "- If no rule was violated, output an empty object: {{}}.\n"
    "- Markdown code fences, prose, explanations, and nested structures\n"
    "  are all forbidden: output a single flat JSON object and nothing else.\n"
)

_STEP_BY_STEP_DIRECTIVE = (
    "Think step-by-step, carefully and in depth, about what the customer\n"
    "needed, what the agent actually did, and which rules were violated,\n"
    "BEFORE producing the final answer. Keep that reasoning internal: it\n"
    "must NOT appear in the output.\n"
)


def _build_scoring_prompt(
    rules: Sequence[tuple[str, str, int, str]],
    base_system_text: str | None = None,
) -> str:
    """Build the scoring system prompt.

    ``base_system_text`` is the DB-stored active system prompt for the
    ``"ai_scoring"`` key; when no active row exists it falls back to
    the hardcoded ``SYSTEM_PROMPT`` constant (identical behavior to
    before the system-prompt feature). With configured rules, the rule
    lines and the shared JSON-output contract are appended to the base
    text; with no rules the base text is returned unchanged.

    Each rule is ``(error_name, display_name, penalty_points,
    category)``. Rule lines are grouped under their category as short
    section headers so the model sees the taxonomy; blank categories
    fall under "General". The output contract stays flat: the model
    still returns a single ``{error_key: deduction}`` object.
    """
    base = base_system_text if base_system_text else SYSTEM_PROMPT

    if not rules:
        return base

    # dict preserves first-appearance order of the categories (rules
    # arrive ordered by template id, then error name).
    lines_by_category: dict[str, list[str]] = {}
    for error_name, display_name, penalty, category in rules:
        header = category if category and category.strip() else "General"
        lines_by_category.setdefault(header, []).append(
            f"- {error_name} ({display_name}): deduct {penalty} point(s) when violated."
        )

    rule_lines: list[str] = []
    for category, lines in lines_by_category.items():
        rule_lines.append(f"{category}:")
        rule_lines.extend(lines)

    penalty_map = ", ".join(
        f"{name}: {points}"
        for name, _, points, _ in rules
    )

    return (
        base.rstrip("\n")
        + "\n\n"
        + "You will receive the rich-text (HTML) QA notes about a support agent's\n"
        "case. Judge each of the configured scoring rules below:\n"
        "\n"
        + "\n".join(rule_lines)
        + "\n"
        "\n"
        + _STEP_BY_STEP_DIRECTIVE
        + _JSON_OUTPUT_CONTRACT.format(penalty_map=penalty_map)
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AnalyzeError(Exception):
    """The AI analysis call failed or its response could not be parsed."""


# ---------------------------------------------------------------------------
# OpenRouter client
# ---------------------------------------------------------------------------

@lru_cache
def get_openrouter_client() -> AsyncOpenAI:
    """Return the cached AsyncOpenAI client pointed at OpenRouter.

    Created lazily and cached so importing this module never crashes when
    ``OPENROUTER_API_KEY`` is unset.
    """
    settings = get_settings()

    if settings.OPENROUTER_API_KEY is None:
        raise ValueError(
            "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
            "in the environment or the .env file to enable AI auto-scoring."
        )

    return AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=settings.OPENROUTER_API_KEY,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Return ``text`` with surrounding markdown code fences removed."""
    cleaned = text.strip()

    match = _CODE_FENCE_RE.match(cleaned)

    if match is not None:
        return match.group(1).strip()

    return cleaned


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of a raw model response."""
    if not isinstance(text, str):
        raise AnalyzeError(
            f"Model returned non-string content: {repr(text)[:200]}"
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return parsed

    stripped = _strip_code_fences(text)

    if stripped != text:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            return parsed

    raise AnalyzeError(
        "Could not parse the model response as a flat JSON object. "
        f"Raw response snippet: {text[:200]!r}"
    )


def _sanitize_scorecard(data: dict) -> dict[str, int]:
    """Coerce a parsed model scorecard into ``{error_key: int}``."""
    if not isinstance(data, dict):
        return {}

    sanitized: dict[str, int] = {}

    for key, value in data.items():
        if not isinstance(key, str):
            continue

        if len(key) > MAX_KEY_LENGTH:
            continue

        if isinstance(value, bool):
            continue

        if not isinstance(value, (int, float)):
            continue

        if isinstance(value, float) and not math.isfinite(value):
            continue

        deduction = int(round(value))

        if deduction < 0:
            deduction = 0
        elif deduction > MAX_DEDUCTION:
            deduction = MAX_DEDUCTION

        sanitized[key] = deduction

    return sanitized


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------

def _log_openrouter_error(exc: Exception) -> None:
    """Log as much useful information as possible from an OpenRouter error."""
    logger.exception(
        "OpenRouter request failed: type=%s status_code=%s message=%s",
        type(exc).__name__,
        getattr(exc, "status_code", None),
        str(exc),
    )

    body = getattr(exc, "body", None)

    if body is not None:
        logger.error("OpenRouter error body: %r", body)

    response = getattr(exc, "response", None)

    if response is not None:
        try:
            logger.error(
                "OpenRouter HTTP response: status=%s body=%s",
                getattr(response, "status_code", None),
                response.text,
            )
        except Exception:
            logger.error("Unable to read OpenRouter HTTP response body.")


# ---------------------------------------------------------------------------
# Score analysis
# ---------------------------------------------------------------------------

async def _active_system_prompt(
    key: str, db_session: AsyncSession
) -> str | None:
    """Return the newest ACTIVE prompt content for ``key``, or None.

    "Newest" = highest ``created_at``, ties broken by higher ``id``.
    """
    stmt = (
        select(SystemPrompt.content)
        .where(
            SystemPrompt.key == key,
            SystemPrompt.is_active.is_(True),
        )
        .order_by(
            SystemPrompt.created_at.desc(),
            SystemPrompt.id.desc(),
        )
        .limit(1)
    )
    return (await db_session.execute(stmt)).scalar_one_or_none()


async def analyze_support_ticket(
    notes_html: str,
    case_type: CaseTypeEnum,
    db_session: AsyncSession,
) -> dict[str, int]:
    """Analyze rich-text (HTML) QA notes into a raw scorecard."""
    stmt = (
        select(ScorecardItem)
        .join(
            ScorecardTemplate,
            ScorecardItem.template_id == ScorecardTemplate.id,
        )
        .where(
            ScorecardTemplate.case_type == case_type,
            ScorecardItem.is_active.is_(True),
        )
        .order_by(
            ScorecardTemplate.id,
            ScorecardItem.error_name,
        )
    )

    items = list(
        (await db_session.execute(stmt))
        .scalars()
        .all()
    )

    rules_by_key: dict[str, tuple[str, str, int, str]] = {}

    for item in items:
        rules_by_key.setdefault(
            item.error_name,
            (
                item.error_name,
                item.display_name,
                item.penalty_points,
                # Defensive: rows predating the category column (or a
                # blank label) are grouped under "General".
                (item.category or "").strip() or "General",
            ),
        )

    rules = list(rules_by_key.values())

    # DB-stored prompt wins; the hardcoded constant is only a fallback
    # for an empty/inactive system_prompts table.
    db_prompt = await _active_system_prompt(SCORING_PROMPT_KEY, db_session)
    system_prompt = _build_scoring_prompt(rules, base_system_text=db_prompt)

    # Release the DB connection before the multi-second network request.
    await db_session.commit()

    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        f"Case type: {case_type.value}\n\n"
                        f"QA notes (HTML):\n\n{notes_html}"
                    ),
                },
            ],
            response_format={
                "type": "json_object",
            },
            temperature=0,
            extra_body={
                "provider": OPENROUTER_PROVIDER,
            },
        )

    except APIError as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter API error: {exc}"
        ) from exc

    except Exception as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter request failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not response.choices:
        raise AnalyzeError(
            "OpenRouter returned no choices."
        )

    content = response.choices[0].message.content

    if not content:
        raise AnalyzeError(
            "OpenRouter returned an empty response."
        )

    parsed = _extract_json(content)

    sanitized = _sanitize_scorecard(parsed)

    if rules:
        allowed = set(rules_by_key)

        sanitized = {
            key: value
            for key, value in sanitized.items()
            if key in allowed
        }

    return sanitized


# ---------------------------------------------------------------------------
# Refactor
# ---------------------------------------------------------------------------

async def refactor_qa_notes(raw_html: str, db_session: AsyncSession) -> str:
    """Rewrite QA notes for clarity, grammar and professional tone.

    The base system prompt is the newest ACTIVE SystemPrompt row for
    ``REFACTOR_PROMPT_KEY``; the hardcoded ``REFACTOR_SYSTEM_PROMPT``
    constant is only the fallback for an empty/inactive table
    (identical behavior to before the system-prompt feature).
    """
    # DB-stored prompt wins; the hardcoded constant is only a fallback.
    db_prompt = await _active_system_prompt(REFACTOR_PROMPT_KEY, db_session)
    system_prompt = db_prompt if db_prompt else REFACTOR_SYSTEM_PROMPT

    # Release the DB connection before the multi-second network request
    # (same commit-then-call pattern as analyze_support_ticket).
    await db_session.commit()

    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": raw_html,
                },
            ],
            temperature=0,
            extra_body={
                "provider": OPENROUTER_PROVIDER,
            },
        )

    except APIError as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter API error: {exc}"
        ) from exc

    except Exception as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter request failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not response.choices:
        raise AnalyzeError(
            "OpenRouter returned no choices."
        )

    content = response.choices[0].message.content

    if not content:
        raise AnalyzeError(
            "OpenRouter returned an empty response."
        )

    return _strip_code_fences(content)


# ---------------------------------------------------------------------------
# Notes from score
# ---------------------------------------------------------------------------

async def draft_notes_from_score(
    case_type: CaseTypeEnum,
    raw_scorecard: dict[str, int],
    db_session: AsyncSession,
    support_agent_id: int | None = None,
    exclude_review_id: int | None = None,
    no_multiplier_keys: set[str] | None = None,
) -> str:
    """Draft review notes (sanitized HTML fragment) from ticked deductions.

    The base system prompt is the newest ACTIVE SystemPrompt row for
    ``NOTES_FROM_SCORE_PROMPT_KEY``; the hardcoded
    ``NOTES_FROM_SCORE_SYSTEM_PROMPT`` constant is only the fallback
    (identical behavior to the other AI capabilities).

    Deduction keys missing from the active rules of ``case_type`` are
    skipped silently so stale client payloads cannot crash the call.
    When ``support_agent_id`` is given, progressive multipliers are
    computed for that agent (same engine as saving a review — see
    ``multiplier_service.calculate_final_score``, including
    ``exclude_review_id`` and ``no_multiplier_keys`` semantics) and the
    user message carries the amplified per-rule penalties plus the
    final score, so the drafted notes justify the real number.
    The output is NOT sanitized server-side — the client sanitizes it
    with DOMPurify before insertion (same trust boundary as
    ``refactor_qa_notes``).
    """
    rules_snapshot = await get_active_rules(case_type, db_session)
    items = rules_snapshot["items"]
    rules_by_key = {item["error_name"]: item for item in items}

    total_rules = len(items)
    known_raw: dict[str, int] = {
        error_key: deduction
        for error_key, deduction in raw_scorecard.items()
        if error_key in rules_by_key
    }

    # Multiplier context: identical math to what saving the review will
    # produce, so the notes match the persisted final score. Without an
    # agent (or with no rules matched) the request degrades gracefully
    # to raw deductions only.
    breakdown: dict[str, dict[str, int]] = {}
    final_score: int | None = None
    if support_agent_id is not None:
        (
            breakdown,
            final_score,
            _total_penalty,
        ) = await multiplier_service.calculate_final_score(
            support_agent_id,
            known_raw,
            db_session,
            exclude_review_id=exclude_review_id,
            no_multiplier_keys=no_multiplier_keys,
        )

    deducted_lines: list[str] = []
    for error_key, deduction in known_raw.items():
        rule = rules_by_key[error_key]
        category = str(rule.get("category") or "").strip() or "General"
        entry = breakdown.get(error_key) or {}
        multiplier = entry.get("multiplier")
        final_penalty = entry.get("final_penalty")
        if multiplier is not None and final_penalty is not None:
            deducted_lines.append(
                f"- {rule['display_name']} ({category}) "
                f"−{deduction} ×{multiplier} → −{final_penalty}"
            )
        else:
            deducted_lines.append(
                f"- {rule['display_name']} ({category}) −{deduction}"
            )

    clean_count = total_rules - len(deducted_lines)

    user_parts = [f"Case type: {case_type.value}"]
    if deducted_lines:
        user_parts.append("Deducted rules:")
        user_parts.extend(deducted_lines)
    else:
        user_parts.append(
            "No violations were ticked: the raw scorecard is empty."
        )
    user_parts.append(f"{clean_count} of {total_rules} rules stayed clean.")
    if final_score is not None:
        user_parts.append(
            f"Final score after progressive multipliers: "
            f"{final_score}/100 (a repeated recent mistake counts "
            f"several times; '×1' means first occurrence or waived)."
        )

    # DB-stored prompt wins; the hardcoded constant is only a fallback.
    db_prompt = await _active_system_prompt(NOTES_FROM_SCORE_PROMPT_KEY, db_session)
    system_prompt = db_prompt if db_prompt else NOTES_FROM_SCORE_SYSTEM_PROMPT

    # Release the DB connection before the multi-second network request
    # (same commit-then-call pattern as the scoring/refactor flows).
    await db_session.commit()

    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": "\n".join(user_parts),
                },
            ],
            temperature=0,
            extra_body={
                "provider": OPENROUTER_PROVIDER,
            },
        )

    except APIError as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter API error: {exc}"
        ) from exc

    except Exception as exc:
        _log_openrouter_error(exc)
        raise AnalyzeError(
            f"OpenRouter request failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not response.choices:
        raise AnalyzeError(
            "OpenRouter returned no choices."
        )

    content = response.choices[0].message.content

    if not content:
        raise AnalyzeError(
            "OpenRouter returned an empty response."
        )

    notes_html = _strip_code_fences(content)

    if not notes_html:
        raise AnalyzeError(
            "OpenRouter returned an empty response."
        )

    return notes_html
