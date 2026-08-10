"""The Python and SQL business day implementations must agree (NFR-05).

The rule exists twice: `permitflow/process/sla.py` for the engine, and
`business_days_between` in `sql/001_schema.sql` for the reporting views. Two copies of one
rule is a real risk, and the failure mode is quiet: the engine would stamp a due date the
compliance report disagrees with, and nobody would notice until someone reconciled them
by hand.

This test is the thing that makes keeping both copies acceptable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from permitflow.config import DEPARTMENT_TZ
from permitflow.db import load_holiday_calendar
from permitflow.process.sla import add_business_days, business_days_between


def _date_pairs() -> list[tuple[date, date]]:
    """Pairs spanning weekends, holidays, and the turn of the year.

    Generated rather than hand-listed so the coverage is broad, but deterministic so a
    failure is reproducible.
    """
    anchors = [
        date(2026, 1, 1),   # New Year's Day, a Thursday
        date(2026, 1, 16),  # Friday before the MLK long weekend
        date(2026, 6, 17),  # Two days before Juneteenth
        date(2026, 7, 1),   # Week containing the observed July 4 holiday
        date(2026, 8, 3),   # Ordinary Monday
        date(2026, 8, 7),   # Ordinary Friday
        date(2026, 11, 24), # Thanksgiving week, two consecutive holidays
        date(2026, 12, 23), # Christmas week
        date(2026, 12, 28), # Crossing into the next year
    ]
    spans = [0, 1, 2, 3, 4, 5, 7, 10, 14, 20, 30, 45]
    return [(a, a + timedelta(days=s)) for a in anchors for s in spans]


@pytest.fixture(scope="module")
def pairs() -> list[tuple[date, date]]:
    return _date_pairs()


def _as_instant(d: date) -> datetime:
    return datetime.combine(d, time(9, 0), tzinfo=DEPARTMENT_TZ)


def test_business_days_between_matches_sql(conn, pairs) -> None:
    calendar = load_holiday_calendar(conn)
    mismatches: list[str] = []

    with conn.cursor() as cur:
        for start, end in pairs:
            python_result = business_days_between(_as_instant(start), _as_instant(end), calendar)
            cur.execute(
                "SELECT business_days_between(%s, %s) AS n", (_as_instant(start), _as_instant(end))
            )
            sql_result = cur.fetchone()["n"]
            if python_result != sql_result:
                mismatches.append(f"{start} to {end}: python={python_result} sql={sql_result}")

    assert not mismatches, "implementations disagree:\n" + "\n".join(mismatches)


def test_add_business_days_matches_sql(conn, pairs) -> None:
    calendar = load_holiday_calendar(conn)
    mismatches: list[str] = []

    with conn.cursor() as cur:
        for start, _ in pairs:
            for days in (0, 1, 3, 5, 14, 22):
                python_result = add_business_days(_as_instant(start), days, calendar).date()
                cur.execute("SELECT add_business_days(%s, %s) AS d", (_as_instant(start), days))
                sql_result = cur.fetchone()["d"].astimezone(DEPARTMENT_TZ).date()
                if python_result != sql_result:
                    mismatches.append(
                        f"{start} + {days}d: python={python_result} sql={sql_result}"
                    )

    assert not mismatches, "implementations disagree:\n" + "\n".join(mismatches)


def test_holiday_calendar_is_loaded_from_the_database(conn) -> None:
    """The two implementations must be reading the same holiday list, not two copies."""
    calendar = load_holiday_calendar(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM holiday")
        assert len(calendar.dates) == cur.fetchone()["n"]
    assert date(2026, 7, 3) in calendar.dates  # observed Independence Day
    assert calendar.is_business_day(date(2026, 7, 3)) is False
    assert calendar.is_business_day(date(2026, 8, 3)) is True
