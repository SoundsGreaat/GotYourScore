"""Custom reporting calendar and pacing intervals.

Business rules (all datetimes UTC):

- A reporting period starts on the 26th of one month (00:00 UTC) and
  ends on the 25th of the NEXT month (exclusive end: the 26th of the
  closing month at 00:00 UTC).
- A period is NAMED after its CLOSING month: Aug 26 – Sep 25 belongs to
  "September".
- Each period contains exactly six pacing intervals (half-open
  ``[start, end)``), one review per assigned agent each:

      I1: 26th -> 1st   ("26–31" / "26–30" / ... actual end day)
      I2:  1st -> 6th   ("1–5")
      I3:  6th -> 11th  ("6–10")
      I4: 11th -> 16th  ("11–15")
      I5: 16th -> 21st  ("16–20")
      I6: 21st -> 26th  ("21–25")

  Six intervals x one review per agent = the MONTHLY_QUOTA of 6.

All functions are pure and unit-testable; December/January rollovers
and leap-year Februarys are handled via explicit month arithmetic.
"""

import calendar
from datetime import datetime, timezone

# English closing-month names, hardcoded so labels never depend on the
# process locale.
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# En dash used in interval labels ("26–31").
_DASH = "\u2013"


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift a (year, month) pair by ``delta`` months, rolling over years."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def reporting_period_bounds(
    closing_year: int, closing_month: int
) -> tuple[datetime, datetime]:
    """Return ``(period_start_utc, period_end_utc_exclusive)`` for the
    period identified by its CLOSING (year, month)."""
    prev_year, prev_month = _add_months(closing_year, closing_month, -1)
    return (
        datetime(prev_year, prev_month, 26, tzinfo=timezone.utc),
        # Exclusive end = the 26th of the CLOSING month (right after its
        # 25th ends) — NOT the successor month, which would overlap the
        # next period entirely.
        datetime(closing_year, closing_month, 26, tzinfo=timezone.utc),
    )


def reporting_period_for(dt: datetime) -> tuple[datetime, datetime, int, int]:
    """Map a moment to the reporting period it falls into.

    Args:
        dt: any aware or naive datetime (naive is treated as UTC).

    Returns:
        ``(period_start_utc, period_end_utc_exclusive, closing_year,
        closing_month)``. Days 26-31 belong to the NEXT month's period;
        days 1-25 belong to the current month's period.
    """
    dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(
        tzinfo=timezone.utc
    )
    if dt_utc.day >= 26:
        closing_year, closing_month = _add_months(dt_utc.year, dt_utc.month, 1)
    else:
        closing_year, closing_month = dt_utc.year, dt_utc.month
    start, end = reporting_period_bounds(closing_year, closing_month)
    return start, end, closing_year, closing_month


def pacing_intervals(
    closing_year: int, closing_month: int
) -> list[tuple[str, datetime, datetime]]:
    """Return the six pacing intervals of a reporting period.

    Args:
        closing_year: year of the period's CLOSING month.
        closing_month: month of the period's CLOSING month (1-12).

    Returns:
        Ordered list of ``(label, start_utc, end_utc_exclusive)`` tuples
        covering the whole period back-to-back. The first label reflects
        the ACTUAL last day of the opening month ("26–31", "26–30",
        "26–29" in leap years, ...).
    """
    prev_year, prev_month = _add_months(closing_year, closing_month, -1)

    opening_days = calendar.monthrange(prev_year, prev_month)[1]
    boundaries = [
        datetime(closing_year, closing_month, 1, tzinfo=timezone.utc),
        datetime(closing_year, closing_month, 6, tzinfo=timezone.utc),
        datetime(closing_year, closing_month, 11, tzinfo=timezone.utc),
        datetime(closing_year, closing_month, 16, tzinfo=timezone.utc),
        datetime(closing_year, closing_month, 21, tzinfo=timezone.utc),
        # Exclusive end of I6 = the 26th of the CLOSING month.
        datetime(closing_year, closing_month, 26, tzinfo=timezone.utc),
    ]
    labels = [
        f"26{_DASH}{opening_days}",
        f"1{_DASH}5",
        f"6{_DASH}10",
        f"11{_DASH}15",
        f"16{_DASH}20",
        f"21{_DASH}25",
    ]

    intervals: list[tuple[str, datetime, datetime]] = []
    cursor = datetime(prev_year, prev_month, 26, tzinfo=timezone.utc)
    for label, boundary in zip(labels, boundaries):
        intervals.append((label, cursor, boundary))
        cursor = boundary
    return intervals


def period_label(closing_year: int, closing_month: int) -> str:
    """Human-readable period name, e.g. ``"September 2026"``."""
    return f"{_MONTH_NAMES[closing_month - 1]} {closing_year}"
