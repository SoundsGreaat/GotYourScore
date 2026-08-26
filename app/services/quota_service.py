"""Quota service over the custom reporting calendar.

Reporting periods (see ``app.services.reporting_period``) run from the
26th of one month to the 25th of the next and are NAMED after their
CLOSING month. Quota rule: a support agent may receive at most
``MONTHLY_QUOTA`` reviews per REPORTING period. "Completed" counts ALL
Review records for the agent within the period's half-open
``[start, end)`` range on ``created_at`` — including 'No Cases'
reviews. Period boundaries are UTC (the engine pins the session
timezone to UTC).

Exclusions applied to every count in this module (see
:func:`counted_review_filters`):

- soft-deleted rows (``deleted_at`` set) never count;
- ``pending`` rows (delegated handoffs awaiting their assigned QA) do
  not count until completed.

QA compliance (``get_qa_compliance``) applies the same calendar to a
QA's assignment SCOPE, and credit is SCOPE-BASED, not
performer-based: every counted review of one of the QA's assigned
agents credits EVERY QA whose scope covers that agent, regardless of
which QA actually performed it (DB attribution via ``Review.qa_id``
is untouched, so one review can credit several QAs when scopes
overlap). Each pacing interval still requires one review per assigned
agent (pacing/notification lens), while period totals measure overall
progress against ``len(assigned agents) * MONTHLY_QUOTA``.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import QAAssignment, Review, ReviewStatusEnum, User
from app.services.reporting_period import (
    pacing_intervals,
    reporting_period_bounds,
)


def counted_review_filters() -> Sequence[ColumnElement[bool]]:
    """Shared WHERE fragments for every quota/compliance count.

    Both quota counts and compliance counts must agree on what a
    "counted" review is:

    - rows soft-deleted (``deleted_at`` set) are invisible to reporting;
    - rows with ``status='pending'`` do not count towards quotas until
      their assigned QA completes them (the handoff itself is
      quota-neutral).

    Target math is untouched — only the numerator (completed counts)
    narrows.
    """
    return (
        Review.deleted_at.is_(None),
        Review.status == ReviewStatusEnum.COMPLETED,
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
        comes from ``settings.MONTHLY_QUOTA``. Soft-deleted and
        ``pending`` reviews are excluded from ``completed`` (see
        :func:`counted_review_filters`).
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
            *counted_review_filters(),
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
    """Scope-based quota compliance for one QA over a reporting period.

    Compliance credit is SCOPE-BASED, not performer-based: every
    counted review of one of this QA's assigned agents counts toward
    this QA's progress, regardless of which QA actually performed it.
    The DB keeps the real ``qa_id`` as reviewer of record (My Reviews
    of the performer is untouched); a single review can credit several
    QAs at once when their assignment scopes overlap.

    Assigned agents are the DISTINCT support_agent_id values of this
    QA's assignments whose user is still active (``deleted_at IS
    NULL``): a soft-deleted support agent no longer contributes to
    required/completed, while their historical reviews stay intact.
    ``total_required`` is ``len(assigned agents) * MONTHLY_QUOTA``;
    ``total_completed`` is a plain COUNT of counted reviews (see
    :func:`counted_review_filters`) for those agents within the
    half-open ``[start, end)`` period range on ``created_at`` — no
    ``qa_id`` filter and no DISTINCT collapse; ``total_deficit`` is
    ``max(0, required - completed)``.

    The per-interval list remains the PACING/NOTIFICATION lens: each
    interval still requires one review per assigned agent, but its
    ``completed`` is likewise a plain COUNT of counted reviews inside
    the interval (no ``qa_id`` filter, no DISTINCT collapse). Totals
    deliberately come from the PERIOD query, NOT from summing the
    intervals: intervals answer "what should have happened by now"
    (pacing), while totals answer "how far along is the whole period"
    (progress) — e.g. extra reviews early in the period keep totals on
    track even though some earlier interval shows a deficit.

    Soft-deleted and ``pending`` reviews are excluded from every
    completed count. This function does NOT commit — it only reads.

    Returns:
        ``{"assigned_agent_count", "intervals", "total_required",
        "total_completed", "total_deficit"}`` where ``intervals`` is a
        list of dicts with ``label/starts_at/ends_at/required/
        completed/deficit``.
    """
    assigned = (
        await db_session.execute(
            select(QAAssignment.support_agent_id)
            .join(User, User.id == QAAssignment.support_agent_id)
            .where(
                QAAssignment.qa_id == qa_id,
                # Soft-deleted agents leave the compliance math; their
                # historical reviews remain untouched in the DB.
                User.active_filter(),
            )
            .distinct()
        )
    ).scalars().all()
    agent_ids = sorted(set(assigned))

    total_required = len(agent_ids) * get_settings().MONTHLY_QUOTA

    total_completed = 0
    if agent_ids:
        # Progress lens: one PERIOD query over the whole reporting
        # window — NOT the sum of interval completions (see docstring).
        period_start, period_end = reporting_period_bounds(
            closing_year, closing_month
        )
        total_completed = int(
            (
                await db_session.execute(
                    select(func.count())
                    .select_from(Review)
                    .where(
                        Review.support_agent_id.in_(agent_ids),
                        Review.created_at >= period_start,
                        Review.created_at < period_end,
                        *counted_review_filters(),
                    )
                )
            ).scalar_one()
        )

    intervals: list[dict[str, Any]] = []
    for label, start, end in pacing_intervals(closing_year, closing_month):
        required = len(agent_ids)
        completed = 0
        if agent_ids:
            completed = int(
                (
                    await db_session.execute(
                        select(func.count())
                        .select_from(Review)
                        .where(
                            Review.support_agent_id.in_(agent_ids),
                            Review.created_at >= start,
                            Review.created_at < end,
                            *counted_review_filters(),
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

    return {
        "assigned_agent_count": len(agent_ids),
        "intervals": intervals,
        "total_required": total_required,
        "total_completed": total_completed,
        "total_deficit": max(0, total_required - total_completed),
    }
