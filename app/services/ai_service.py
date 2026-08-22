"""AI services (OpenRouter via the official OpenAI SDK).

Two capabilities:
- ``analyze_support_ticket``: score rich-text (HTML) QA notes against
  the scorecard rules configured for the case type (ScorecardTemplate
  / ScorecardItem), falling back to a generic error list when no
  rules are configured;
- ``refactor_qa_notes``: rewrite QA notes for clarity, grammar and
  professional tone while preserving the HTML markup and embedded
  images.

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
    rules: Sequence[tuple[str, str, int]],
    base_system_text: str | None = None,
) -> str:
    """Build the scoring system prompt.

    ``base_system_text`` is the DB-stored active system prompt for the
    ``"ai_scoring"`` key; when no active row exists it falls back to
    the hardcoded ``SYSTEM_PROMPT`` constant (identical behavior to
    before the system-prompt feature). With configured rules, the rule
    lines and the shared JSON-output contract are appended to the base
    text; with no rules the base text is returned unchanged.
    """
    base = base_system_text if base_system_text else SYSTEM_PROMPT

    if not rules:
        return base

    rule_lines = [
        f"- {error_name} ({display_name}): deduct {penalty} point(s) when violated."
        for error_name, display_name, penalty in rules
    ]

    penalty_map = ", ".join(
        f"{name}: {points}"
        for name, _, points in rules
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

    rules_by_key: dict[str, tuple[str, str, int]] = {}

    for item in items:
        rules_by_key.setdefault(
            item.error_name,
            (
                item.error_name,
                item.display_name,
                item.penalty_points,
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

async def refactor_qa_notes(raw_html: str) -> str:
    """Rewrite QA notes for clarity, grammar and professional tone."""
    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": REFACTOR_SYSTEM_PROMPT,
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
