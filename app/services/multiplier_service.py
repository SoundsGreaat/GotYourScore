"""Progressive multiplier scoring service.

Business rules:
- Base score is 100; the final score is floored at 0.
- For each error key with a positive deduction, the penalty is multiplied
  by the number of past occurrences of that same error key (entries with
  ``deducted > 0``) in the support agent's PAST reviews — all time, any
  case type. 'No Cases' reviews have null ``scorecard_data`` and are
  therefore never counted.
- Multiplier for the current submission = past_occurrences + 1, so the
  first occurrence is penalized 1x, the second 2x, and so on.

Stored shape compatibility: reviews saved since the rules-snapshot
feature nest the breakdown under a top-level ``"breakdown"`` key;
legacy rows store the flat breakdown at the top level. The past-
occurrence scan accepts both shapes (see ``_stored_breakdown``), so
progressive multipliers keep working across old and new rows.
"""

from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Review

BASE_SCORE: int = 100
MIN_FINAL_SCORE: int = 0


def _stored_breakdown(scorecard: Any) -> dict:
    """Return the breakdown dict from one stored scorecard JSONB.

    New rows look like ``{"rules_snapshot": ..., "breakdown": {...}}``;
    legacy rows are the flat breakdown itself. A dict-valued top-level
    ``"breakdown"`` key decides which shape we are looking at.
    """
    if not isinstance(scorecard, dict):
        return {}
    nested = scorecard.get("breakdown")
    if isinstance(nested, dict):
        return nested
    return scorecard


def _iter_stored_deductions(scorecard: Any) -> Iterator[tuple[str, int]]:
    """Yield ``(error_key, deducted)`` pairs from a stored scorecard."""
    for error_key, entry in _stored_breakdown(scorecard).items():
        if not isinstance(entry, dict):
            continue
        try:
            deducted = int(entry.get("deducted") or 0)
        except (TypeError, ValueError):
            continue
        yield error_key, deducted


async def calculate_final_score(
    agent_id: int,
    raw_scorecard: dict[str, int],
    db_session: AsyncSession,
) -> tuple[dict[str, dict[str, int]], int, int]:
    """Compute the detailed breakdown and scores for one review.

    Args:
        agent_id: id of the support agent being reviewed.
        raw_scorecard: mapping of error key -> raw deduction points
            (e.g. ``{"late_response": 5}``). Deductions are whole
            numbers; fractional/negative values are rejected upstream
            by the schema; zero means "no error".
        db_session: async SQLAlchemy session used for reading past
            reviews. This function does NOT commit — the caller owns
            the transaction.

    Returns:
        Tuple ``(breakdown, final_score, total_penalty)`` where
        ``breakdown`` is ``{"<error_key>": {"deducted": X,
        "multiplier": Y, "final_penalty": Z}}`` (all whole numbers),
        ``final_score`` is ``max(0, 100 - total_penalty)`` and
        ``total_penalty`` is the sum of all final penalties.

    Zero-deduction entries are kept in the output for transparency as
    ``{"deducted": 0, "multiplier": 1, "final_penalty": 0}``, but they do
    NOT increment occurrence counts (only ``deducted > 0`` counts).
    """
    past_scorecards = (
        await db_session.execute(
            select(Review.scorecard_data).where(
                Review.support_agent_id == agent_id,
                Review.scorecard_data.is_not(None),
                # Soft-deleted reviews must not inflate progressive
                # multipliers (deleting a mistake erases its occurrence
                # history too). Pending handoffs always carry a null
                # scorecard, so they are excluded by the IS NOT NULL
                # check above by construction.
                Review.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    # Count past occurrences per error key (only entries with deducted > 0).
    occurrences: dict[str, int] = {}
    for scorecard in past_scorecards:
        for error_key, deducted in _iter_stored_deductions(scorecard):
            if deducted > 0:
                occurrences[error_key] = occurrences.get(error_key, 0) + 1

    detailed: dict[str, dict[str, int]] = {}
    total_penalty = 0
    for error_key, raw_deducted in raw_scorecard.items():
        deducted = int(raw_deducted)
        if deducted > 0:
            multiplier = occurrences.get(error_key, 0) + 1
            final_penalty = deducted * multiplier
        else:
            # Zero deduction: reported for transparency, never counted
            # as an occurrence and never penalized.
            multiplier = 1
            final_penalty = 0
        detailed[error_key] = {
            "deducted": deducted,
            "multiplier": multiplier,
            "final_penalty": final_penalty,
        }
        total_penalty += final_penalty

    final_score = max(MIN_FINAL_SCORE, BASE_SCORE - total_penalty)
    return detailed, final_score, total_penalty
