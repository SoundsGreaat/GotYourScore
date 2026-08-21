"""AI auto-scoring service (OpenRouter via the official OpenAI SDK).

OpenRouter exposes an OpenAI-compatible API, so the official ``openai``
package is used with a ``base_url`` override. The client is created
lazily and cached — mirroring ``app.core.security.get_oauth`` — so
importing this module never crashes when ``OPENROUTER_API_KEY`` is
unset.

Error contract for callers (see the reviews endpoint):
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

from openai import APIError, AsyncOpenAI

from app.core.config import get_settings

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

You will receive the transcript of a support agent's ticket. Analyze the
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


async def analyze_support_ticket(transcript: str) -> dict[str, int]:
    """Analyze a support ticket transcript into a raw scorecard.

    Sends the transcript to the OpenRouter-hosted scoring model with
    JSON mode enforced, then parses and sanitizes the response into
    ``{snake_case_error: deduction_points}`` (whole numbers >= 0; an
    empty dict means "no errors found").

    Raises:
        ValueError: ``OPENROUTER_API_KEY`` is not configured (the
            endpoint maps this to HTTP 503).
        AnalyzeError: the API call failed or the response could not be
            parsed (the endpoint maps this to HTTP 502).
    """
    # Raises ValueError when the key is unset — before any network I/O.
    client = get_openrouter_client()

    try:
        response = await client.chat.completions.create(
            model=AI_SCORING_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Support ticket transcript:\n\n{transcript}",
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
    return _sanitize_scorecard(parsed)
