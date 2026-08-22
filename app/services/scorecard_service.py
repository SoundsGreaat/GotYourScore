"""Scorecard rule snapshot service.

Historical immutability rule: every saved review embeds a snapshot of
the rules that were ACTIVE at save time inside
``reviews.scorecard_data`` (``rules_snapshot``). Later edits,
deactivations or template swaps must never alter past reviews' scores
or breakdowns — scoring always reads the stored snapshot-era values,
never the live rules. This service is the single source of the
snapshot; both review-creation endpoints share it.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CaseTypeEnum, ScorecardItem, ScorecardTemplate


async def get_active_rules(
    case_type: CaseTypeEnum, db_session: AsyncSession
) -> dict[str, object]:
    """Return the snapshot pieces for the currently active rules.

    Only items of ACTIVE templates whose case type matches are
    included. When several active templates define the same
    ``error_name``, the first one wins (templates ordered by id,
    mirroring the AI prompt builder's dedup).

    Returns:
        ``{"case_type": "<value>", "template_ids": [...],
        "items": [{"error_name", "display_name", "penalty_points"},
        ...]}`` — stored verbatim as ``rules_snapshot``.
    """
    stmt = (
        select(ScorecardItem)
        .join(
            ScorecardTemplate,
            ScorecardItem.template_id == ScorecardTemplate.id,
        )
        .where(
            ScorecardTemplate.case_type == case_type,
            ScorecardTemplate.is_active.is_(True),
            ScorecardItem.is_active.is_(True),
        )
        .order_by(
            ScorecardTemplate.id,
            ScorecardItem.error_name,
        )
    )

    items = list((await db_session.execute(stmt)).scalars().all())

    template_ids: list[int] = []
    snapshot_items: list[dict[str, object]] = []
    seen: set[str] = set()

    for item in items:
        if item.template_id not in template_ids:
            template_ids.append(item.template_id)

        # Dedup by error_name ALONE (first template id wins — rows are
        # ordered by template id): the per-template unique constraint
        # already guarantees uniqueness inside one template, so keying
        # on (template_id, error_name) would keep cross-template
        # duplicates that the documented contract forbids.
        if item.error_name in seen:
            continue
        seen.add(item.error_name)

        snapshot_items.append(
            {
                "error_name": item.error_name,
                "display_name": item.display_name,
                "penalty_points": int(item.penalty_points),
            }
        )

    return {
        "case_type": case_type.value,
        "template_ids": template_ids,
        "items": snapshot_items,
    }
