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
) -> tuple[dict[str, Any], int]:
    """Score a review and build a fresh snapshot for storage.

    None leaves multiplier_exemptions out of the JSON (auto-scoring);
    manual scoring records the list even when empty.
    """
    breakdown, final_score, total_penalty = await multiplier_service.calculate_final_score(
        agent_id, raw_scorecard, db,
        exclude_review_id=exclude_review_id,
        no_multiplier_keys=set(multiplier_exemptions or ()),
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
