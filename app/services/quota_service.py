"""Monthly quota service.

Quota rule: a support agent may receive at most ``MONTHLY_QUOTA``
reviews per calendar month. "Completed" counts ALL Review records for
the agent in the given (year, month) based on ``created_at`` —
including 'No Cases' reviews. Month boundaries are UTC (the engine
pins the session timezone to UTC).
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Review


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Return (start, end) UTC datetimes for the given calendar month."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


async def get_agent_quota(
    agent_id: int,
    year: int,
    month: int,
    db_session: AsyncSession,
) -> dict[str, int]:
    """Return the agent's quota status for the given month.

    Args:
        agent_id: id of the support agent.
        year: calendar year (e.g. 2026).
        month: calendar month (1-12).
        db_session: async SQLAlchemy session. This function does NOT
            commit — it only reads.

    Returns:
        ``{"completed": <int>, "target": <int>}`` where ``target`` comes
        from ``settings.MONTHLY_QUOTA``.
    """
    month_start, month_end = _month_bounds(year, month)
    count_stmt = (
        select(func.count())
        .select_from(Review)
        .where(
            Review.support_agent_id == agent_id,
            # Sargable half-open range: uses the created_at index and
            # avoids double EXTRACT (which is not index-friendly).
            Review.created_at >= month_start,
            Review.created_at < month_end,
        )
    )
    completed = (await db_session.execute(count_stmt)).scalar_one()

    return {
        "completed": int(completed),
        "target": get_settings().MONTHLY_QUOTA,
    }
