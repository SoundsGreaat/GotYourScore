"""Progressive multiplier scoring service.

Business rules:
- Base score is 100; the final score is floored at 0.
- For each error key with a positive deduction, the penalty is multiplied
  by the number of past occurrences of that same error key (entries with
  ``deducted > 0``) in the support agent's PAST reviews — any case type,
  but only the agent's LAST SIX scored cases (see ``calculate_final_score``).
  'No Cases' reviews have null ``scorecard_data`` and are therefore never
  counted.
- Multiplier for the current submission = past_occurrences + 1, so the
  first occurrence is penalized 1x, the second 2x, and so on.

Stored shape compatibility: reviews saved since the rules-snapshot
feature nest the breakdown under a top-level ``"breakdown"`` key;
legacy rows store the flat breakdown at the top level. The past-
occurrence scan accepts both shapes (see ``_stored_breakdown``), so
progressive multipliers keep working across old and new rows.
"""

from typing import Any, Iterator

from sqlalchemy import select, true
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
    exclude_review_id: int | None = None,
    no_multiplier_keys: set[str] | None = None,
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

    When ``exclude_review_id`` is given (rescoring an existing row), that
    review is excluded AND the occurrence scan is bounded to reviews
    created strictly EARLIER than it — progressive multipliers must only
    reflect history that predates the reviewed case, never later ones.

    The occurrence history itself is capped to the agent's LAST SIX
    cases preceding this one; older mistakes no longer amplify.

    ``no_multiplier_keys`` waives the progression for specific errors:
    those entries keep ``multiplier: 1`` (penalty = raw deduction) while
    still being recorded. The caller persists the waived keys inside
    ``scorecard_data`` so a later rescore reproduces the same result.
    """
    anchor = None
    if exclude_review_id is not None:
        anchor = await db_session.get(Review, exclude_review_id)
    past_scorecards = (
        (
            await db_session.execute(
                select(Review.scorecard_data)
                .where(
                    Review.support_agent_id == agent_id,
                    Review.scorecard_data.is_not(None),
                    # Rescoring an existing review must not count the row's
                    # OWN stored deductions toward its new multipliers.
                    Review.id != exclude_review_id if exclude_review_id is not None
                    else true(),
                    # Only chronologically earlier reviews count: editing an
                    # OLD review must not pick up deductions from NEWER ones.
                    # Backfilled rows share one timestamp, so equal stamps
                    # are tie-broken by id (lower id = earlier row).
                    (
                        (Review.created_at < anchor.created_at)
                        | (
                            (Review.created_at == anchor.created_at)
                            & (Review.id < exclude_review_id)
                        )
                    )
                    if anchor is not None else true(),
                    # Soft-deleted reviews must not inflate progressive
                    # multipliers (deleting a mistake erases its occurrence
                    # history too). Pending handoffs always carry a null
                    # scorecard, so they are excluded by the IS NOT NULL
                    # check above by construction.
                    Review.deleted_at.is_(None),
                )
                .order_by(Review.created_at.desc())
                .limit(6)  # multipliers draw on the last six cases only
            )
        ).scalars().all()
    )

    # Count past occurrences per error key (only entries with deducted > 0).
    occurrences: dict[str, int] = {}
    for scorecard in past_scorecards:
        for error_key, deducted in _iter_stored_deductions(scorecard):
            if deducted > 0:
                occurrences[error_key] = occurrences.get(error_key, 0) + 1

    detailed: dict[str, dict[str, int]] = {}
    total_penalty = 0
    waived = no_multiplier_keys or set()
    for error_key, raw_deducted in raw_scorecard.items():
        deducted = int(raw_deducted)
        if deducted > 0 and error_key not in waived:
            multiplier = occurrences.get(error_key, 0) + 1
            final_penalty = deducted * multiplier
        else:
            # Zero deduction (reported for transparency, never counted)
            # or a QA-waived progression: multiplier stays at 1.
            multiplier = 1
            final_penalty = deducted if error_key in waived else 0
        detailed[error_key] = {
            "deducted": deducted,
            "multiplier": multiplier,
            "final_penalty": final_penalty,
        }
        total_penalty += final_penalty

    final_score = max(MIN_FINAL_SCORE, BASE_SCORE - total_penalty)
    return detailed, final_score, total_penalty
