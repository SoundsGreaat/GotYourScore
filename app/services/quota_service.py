"""Quota service over the custom reporting calendar.

Reporting periods (see ``app.services.reporting_period``) run from the
26th of one month to the 25th of the next and are NAMED after their
CLOSING month. Quota rule: a support agent may receive at most
``MONTHLY_QUOTA`` reviews per REPORTING period. "Completed" counts ALL
Review records for the agent within the period's half-open
``[start, end)`` range on ``created_at`` — including 'No Cases'
reviews. Period boundaries are UTC (the engine pins the session
timezone to UTC).

QA compliance (``get_qa_compliance``) applies the same calendar to a
reviewer: each pacing interval requires one review per assigned agent,
and the deficit is how many of those required reviews are missing.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import QAAssignment, Review
from app.services.reporting_period import (
    pacing_intervals,
    reporting_period_bounds,
)


async def get_agent_quota(
    agent_id: int,
    year: int,
    month: int,
    db_session: AsyncSession,
) -> dict[str, int]:
    """Return the agent's quota status for one reporting period.

    Args:
        agent_id: id of the support agent.
        year: CLOSING year of the reporting period (e.g. 2026).
        month: CLOSING month of the reporting period (1-12); the period
            spans the 26th of the previous month through the 25th.
        db_session: async SQLAlchemy session. This function does NOT
            commit — it only reads.

    Returns:
        ``{"completed": <int>, "target": <int>}`` where ``target``
        comes from ``settings.MONTHLY_QUOTA``.
    """
    period_start, period_end = reporting_period_bounds(year, month)
    count_stmt = (
        select(func.count())
        .select_from(Review)
        .where(
            Review.support_agent_id == agent_id,
            # Sargable half-open range: uses the created_at index and
            # avoids double EXTRACT (which is not index-friendly).
            Review.created_at >= period_start,
            Review.created_at < period_end,
        )
    )
    completed = (await db_session.execute(count_stmt)).scalar_one()

    return {
        "completed": int(completed),
        "target": get_settings().MONTHLY_QUOTA,
    }


async def get_qa_compliance(
    qa_id: int,
    closing_year: int,
    closing_month: int,
    db_session: AsyncSession,
) -> dict[str, Any]:
    """Per-interval quota compliance for one QA over a reporting period.

    Assigned agents are the DISTINCT non-null support_agent_id values
    of this QA's assignments (General + Hybrid). Each pacing interval
    requires one review per assigned agent; completed counts DISTINCT
    reviewed agents within the half-open interval range, so a QA who
    reviewed the same agent twice in one interval still owes the
    remaining agents. This function does NOT commit — it only reads.

    Returns:
        ``{"assigned_agent_count", "intervals", "total_required",
        "total_completed", "total_deficit"}`` where ``intervals`` is a
        list of dicts with ``label/starts_at/ends_at/required/
        completed/deficit``.
    """
    assigned = (
        await db_session.execute(
            select(QAAssignment.support_agent_id)
            .where(
                QAAssignment.qa_id == qa_id,
                QAAssignment.support_agent_id.is_not(None),
            )
            .distinct()
        )
    ).scalars().all()
    agent_ids = sorted(set(assigned))

    intervals: list[dict[str, Any]] = []
    total_required = 0
    total_completed = 0
    total_deficit = 0

    for label, start, end in pacing_intervals(closing_year, closing_month):
        required = len(agent_ids)
        completed = 0
        if agent_ids:
            completed = int(
                (
                    await db_session.execute(
                        select(func.count(func.distinct(Review.support_agent_id)))
                        .select_from(Review)
                        .where(
                            Review.qa_id == qa_id,
                            Review.support_agent_id.in_(agent_ids),
                            Review.created_at >= start,
                            Review.created_at < end,
                        )
                    )
                ).scalar_one()
            )
        deficit = max(0, required - completed)
        intervals.append(
            {
                "label": label,
                "starts_at": start,
                "ends_at": end,
                "required": required,
                "completed": completed,
                "deficit": deficit,
            }
        )
        total_required += required
        total_completed += completed
        total_deficit += deficit

    return {
        "assigned_agent_count": len(agent_ids),
        "intervals": intervals,
        "total_required": total_required,
        "total_completed": total_completed,
        "total_deficit": total_deficit,
    }
