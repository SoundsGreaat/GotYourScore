"""Shared persistence rules for QA score reviews."""

from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CaseTypeEnum, Review, ReviewStatusEnum
from app.services import multiplier_service, scorecard_service


async def find_pending_pairs(
    db: AsyncSession,
    pairs: list[tuple[int, str]],
    *,
    exclude_review_id: int | None = None,
) -> set[tuple[int, str]]:
    """Look up numbered cases already open, in one query for the whole batch."""
    if not pairs:
        return set()
    stmt = select(Review.support_agent_id, Review.case_number).where(
        tuple_(Review.support_agent_id, Review.case_number).in_(pairs),
        Review.status == ReviewStatusEnum.PENDING,
        Review.deleted_at.is_(None),
    )
    if exclude_review_id is not None:
        stmt = stmt.where(Review.id != exclude_review_id)
    return {(agent_id, number) for agent_id, number in await db.execute(stmt)}


async def build_scorecard_data(
    db: AsyncSession,
    agent_id: int,
    case_type: CaseTypeEnum,
    raw_scorecard: dict[str, int],
    *,
    exclude_review_id: int | None = None,
    multiplier_exemptions: list[str] | None = None,
    autofail: bool = False,
) -> tuple[dict[str, Any], int]:
    """Score a review and build a fresh snapshot for storage.

    None leaves multiplier_exemptions out of the JSON (auto-scoring);
    manual scoring records the list even when empty.
    """
    breakdown, final_score, total_penalty = await calculate_score(
        db, agent_id, raw_scorecard,
        exclude_review_id=exclude_review_id,
        multiplier_exemptions=multiplier_exemptions,
        autofail=autofail,
    )
    data: dict[str, Any] = {
        "rules_snapshot": await scorecard_service.get_active_rules(case_type, db),
        "base_score": multiplier_service.BASE_SCORE,
        "total_penalty": total_penalty,
        "final_score": final_score,
        "breakdown": breakdown,
    }
    if multiplier_exemptions is not None:
        data["multiplier_exemptions"] = multiplier_exemptions
    return data, final_score


def _apply_autofail(final_score: int, autofail: bool) -> int:
    return 0 if autofail else final_score


async def calculate_score(
    db: AsyncSession,
    agent_id: int,
    raw_scorecard: dict[str, int],
    *,
    exclude_review_id: int | None = None,
    multiplier_exemptions: list[str] | None = None,
    autofail: bool = False,
) -> tuple[dict[str, dict[str, int]], int, int]:
    """Calculate the regular deductions, then apply the outcome to the score only."""
    breakdown, regular_score, total_penalty = await multiplier_service.calculate_final_score(
        agent_id, raw_scorecard, db,
        exclude_review_id=exclude_review_id,
        no_multiplier_keys=set(multiplier_exemptions or ()),
    )
    return breakdown, _apply_autofail(regular_score, autofail), total_penalty


def update_stored_outcome(
    data: dict[str, Any], autofail: bool
) -> tuple[dict[str, Any], int]:
    """Apply an outcome-only edit without changing historical deductions or rules."""
    breakdown = multiplier_service.stored_breakdown(data)
    total_penalty = data.get("total_penalty")
    if not isinstance(total_penalty, int):
        # Legacy scorecards store only the flat breakdown.
        total_penalty = sum(
            int(entry.get("final_penalty") or 0)
            for entry in breakdown.values()
            if isinstance(entry, dict)
        )
    final_score = _apply_autofail(multiplier_service.score_after_penalties(total_penalty), autofail)
    if breakdown is not data:
        data = {**data, "final_score": final_score}
    return data, final_score
