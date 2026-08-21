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

Error contract for callers (see the reviews/ai endpoints):
- ``ValueError``: the API key is not configured -> HTTP 503.
- ``AnalyzeError``: the API call failed or the model response could
  not be parsed -> HTTP 502.

JSON extraction and scorecard sanitization are implemented as small
pure helpers (``_extract_json`` / ``_sanitize_scorecard``) so they can
be unit-tested without any network access.
"""

import json
import math
import re
from functools import lru_cache
from typing import Sequence

from openai import APIError, AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import CaseTypeEnum, ScorecardItem, ScorecardTemplate

# OpenRouter's OpenAI-compatible endpoint.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Exact model identifier required by the spec — do not rename.
AI_SCORING_MODEL = "deepseek/deepseek-v4-flash-0731"

# Request bounds: fail fast instead of pinning a worker (and its DB
# connection) on a hanging LLM call; one retry covers transient blips.
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 1

# Sanitizer bounds: scorecards start at 100 points, so a single error
# can never cost more than that; longer keys are data pollution.
MAX_DEDUCTION = 100
MAX_KEY_LENGTH = 64

# A markdown code fence, optionally tagged with a language
# ("```json ... ```" / "``` ... ```"), captured around the payload.
_CODE_FENCE_RE = re.compile(
    r"^```[ \t]*[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```$",
    re.DOTALL,
)

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


def _build_scoring_prompt(rules: Sequence[tuple[str, str, int]]) -> str:
    """Build the scoring system prompt from configured scorecard rules.

    ``rules`` is a sequence of ``(error_name, display_name,
    penalty_points)`` tuples. With no rules the generic fallback
    prompt (``SYSTEM_PROMPT``) is returned unchanged.
    """
    if not rules:
        return SYSTEM_PROMPT

    rule_lines = [
        f"- {error_name} ({display_name}): deduct {penalty} point(s) when violated."
        for error_name, display_name, penalty in rules
    ]
    penalty_map = ", ".join(f"{name}: {points}" for name, _, points in rules)
    return (
        "You are a QA scoring assistant for a customer support team.\n"
        "\n"
        "You will receive the rich-text (HTML) QA notes about a support agent's\n"
        "case. Analyze the agent's performance and judge each of the configured\n"
        "scoring rules below:\n"
        "\n"
        + "\n".join(rule_lines)
        + "\n"
        "\n"
        "Think step-by-step, carefully and in depth, about what the customer\n"
        "needed, what the agent actually did, and which rules were violated,\n"
        "BEFORE producing the final answer. Keep that reasoning internal: it\n"
        "must NOT appear in the output.\n"
        "\n"
        "The final output must be ONLY a JSON object mapping snake_case error\n"
        'names to integer deduction points, e.g. {"late_response": 5}:\n'
        "- Keys are restricted to the error names listed above: unknown keys\n"
        "  are forbidden.\n"
        "- Values MUST match the configured penalty points exactly\n"
        f"  ({penalty_map}); do not invent or adjust deductions.\n"
        "- Use 0 for a rule you considered but found not to be violated.\n"
        "- If no rule was violated, output an empty object: {}.\n"
        "- Markdown code fences, prose, explanations, and nested structures\n"
        "  are all forbidden: output a single flat JSON object and nothing else.\n"
    )


class AnalyzeError(Exception):
    """The AI analysis call failed or its response could not be parsed."""


@lru_cache
def get_openrouter_client() -> AsyncOpenAI:
    """Return the cached AsyncOpenAI client pointed at OpenRouter.

    Created lazily (and cached) so importing this module never crashes
    when ``OPENROUTER_API_KEY`` is unset — the same pattern as
    ``app.core.security.get_oauth``. Raises ``ValueError`` when the key
    is missing so callers (the endpoint) can map it to HTTP 503;
    ``lru_cache`` does not cache exceptions, so a later call after the
    key has been configured succeeds.
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


def _strip_code_fences(text: str) -> str:
    """Return ``text`` with surrounding markdown code fences removed.

    Handles "```json ... ```" and bare "``` ... ```" fences; text
    without fences is returned stripped but otherwise unchanged.
    """
    cleaned = text.strip()
    match = _CODE_FENCE_RE.match(cleaned)
    if match is not None:
        return match.group(1).strip()
    return cleaned


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of a raw model response (pure helper).

    Tries ``json.loads`` on the raw text first; on failure (or when the
    result is not an object) retries once after stripping markdown code
    fences. Raises ``AnalyzeError`` carrying a raw response snippet
    when no JSON object can be recovered.
    """
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
    """Coerce a parsed model scorecard into ``{error_key: int}`` (pure).

    Rules:
    - keep only string keys of at most ``MAX_KEY_LENGTH`` characters
      (longer keys — or prompt-injected junk — are dropped);
    - keep only numeric values: ints and floats are coerced with
      ``int(round(value))`` (Python's round — banker's rounding on
      halves); booleans are dropped first because ``bool`` is a
      subclass of ``int``;
    - drop non-numeric values (strings, None, nested objects/arrays)
      and non-finite floats (NaN / Infinity);
    - clamp deductions into ``[0, MAX_DEDUCTION]`` — negatives to 0,
      anything larger than the 100-point base to ``MAX_DEDUCTION``
      (guards against prompt-inflated magnitudes);
    - KEEP zero deductions: ``multiplier_service`` reports them as
      "no error" transparency entries;
    - a non-dict input yields an empty dict (defensive).
    """
    if not isinstance(data, dict):
        return {}

    sanitized: dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str) or len(key) > MAX_KEY_LENGTH:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
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


async def analyze_support_ticket(
    notes_html: str,
    case_type: CaseTypeEnum,
    db_session: AsyncSession,
) -> dict[str, int]:
    """Analyze rich-text (HTML) QA notes into a raw scorecard.

    Loads the active scorecard rules configured for ``case_type``
    (ScorecardTemplate -> ScorecardItem) and builds the scoring prompt
    from them; with no configured rules the generic fallback prompt
    (``SYSTEM_PROMPT``) is used. The notes HTML is then sent to the
    OpenRouter-hosted scoring model with JSON mode enforced, and the
    response is parsed and sanitized into
    ``{snake_case_error: deduction_points}`` (whole numbers >= 0; an
    empty dict means "no errors found"). When rules are configured,
    the result is additionally filtered to the configured error names
    (the prompt forbids unknown keys; this enforces it server-side).

    The rules queries run first (fast DB reads) and the read
    transaction is then committed so the pooled connection is
    released for the (multi-second) network call.

    Raises:
        ValueError: ``OPENROUTER_API_KEY`` is not configured (the
            endpoint maps this to HTTP 503).
        AnalyzeError: the API call failed or the response could not be
            parsed (the endpoint maps this to HTTP 502).
    """
    stmt = (
        select(ScorecardItem)
        .join(ScorecardTemplate, ScorecardItem.template_id == ScorecardTemplate.id)
        .where(
            ScorecardTemplate.case_type == case_type,
            ScorecardItem.is_active.is_(True),
        )
        .order_by(ScorecardTemplate.id, ScorecardItem.error_name)
    )
    items = list((await db_session.execute(stmt)).scalars().all())
    # Multiple templates may exist for one case type; keep the first
    # occurrence of each error_name so the rule list never contains
    # conflicting duplicates.
    rules_by_key: dict[str, tuple[str, str, int]] = {}
    for item in items:
        rules_by_key.setdefault(
            item.error_name, (item.error_name, item.display_name, item.penalty_points)
        )
    rules = list(rules_by_key.values())
    system_prompt = _build_scoring_prompt(rules)

    # End the read transaction: the pooled connection goes back while
    # the multi-second LLM call runs.
    await db_session.commit()

    # Raises ValueError when the key is unset — before any network I/O.
    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Case type: {case_type.value}\n\n"
                        f"QA notes (HTML):\n\n{notes_html}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,  # deterministic scoring
        )
    except APIError as exc:
        raise AnalyzeError(f"OpenRouter API error: {exc}") from exc
    except Exception as exc:  # network errors, timeouts, SDK failures
        raise AnalyzeError(
            f"OpenRouter request failed: {type(exc).__name__}: {exc}"
        ) from exc

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise AnalyzeError(
            "OpenRouter returned an empty response (no message content)."
        )

    parsed = _extract_json(content)
    sanitized = _sanitize_scorecard(parsed)
    if rules:
        # Rules-based prompts forbid unknown keys; enforce it here too
        # so hallucinated keys never reach the scorecard.
        allowed = set(rules_by_key)
        sanitized = {key: value for key, value in sanitized.items() if key in allowed}
    return sanitized


async def refactor_qa_notes(raw_html: str) -> str:
    """Rewrite QA notes (HTML) for clarity, grammar, and tone.

    Sends the notes HTML to the OpenRouter-hosted model with
    ``REFACTOR_SYSTEM_PROMPT`` (markup and embedded images must be
    preserved verbatim) and returns the improved HTML. The response is
    NOT requested in JSON mode — the output is HTML — but markdown
    code fences are stripped defensively via ``_strip_code_fences``
    should the model wrap its output anyway.

    Raises:
        ValueError: ``OPENROUTER_API_KEY`` is not configured (the
            endpoint maps this to HTTP 503).
        AnalyzeError: the API call failed or the response was empty
            (the endpoint maps this to HTTP 502).
    """
    # Raises ValueError when the key is unset — before any network I/O.
    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {"role": "system", "content": REFACTOR_SYSTEM_PROMPT},
                {"role": "user", "content": raw_html},
            ],
            temperature=0,  # deterministic rewriting
        )
    except APIError as exc:
        raise AnalyzeError(f"OpenRouter API error: {exc}") from exc
    except Exception as exc:  # network errors, timeouts, SDK failures
        raise AnalyzeError(
            f"OpenRouter request failed: {type(exc).__name__}: {exc}"
        ) from exc

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise AnalyzeError(
            "OpenRouter returned an empty response (no message content)."
        )

    return _strip_code_fences(content)
