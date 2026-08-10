"""Business day arithmetic and SLA clock math.

NFR-05 names this as the most tested logic in the system, and the reason is that the
compliance number in FR-23 goes to the city council. A cycle time that silently counts
weekends is a public number that is wrong.

Everything here mirrors `business_days_between` and `add_business_days` in
`sql/001_schema.sql`. Two implementations of one rule is a real risk, so
`tests/test_sla_parity.py` runs both over generated date pairs and asserts they agree.

Interval convention: half open, [start, end). Same instant is zero days. A submission at
09:00 Monday measured at 16:00 Monday has consumed zero business days, which matches how
the department counts and keeps the day of submission from being charged twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..config import DEPARTMENT_TZ

#: End of the department's business day. add_business_days lands here so a due timestamp
#: reads as "close of business on the Nth day" rather than an arbitrary time.
CLOSE_OF_BUSINESS = time(17, 0)


def _local_date(moment: datetime) -> date:
    """Department-local calendar date for an instant.

    Naive datetimes are treated as already local. That is a deliberate convenience for
    tests, which write dates rather than instants. Everything read from the database is
    timezone aware because every column is timestamptz.
    """
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(DEPARTMENT_TZ).date()


@dataclass(frozen=True)
class HolidayCalendar:
    """The published municipal holiday calendar.

    Frozen and hashable so it can be built once per request and passed around without
    anyone being tempted to mutate it mid-calculation.
    """

    dates: frozenset[date]

    @classmethod
    def from_iterable(cls, values) -> HolidayCalendar:
        return cls(frozenset(values))

    @classmethod
    def empty(cls) -> HolidayCalendar:
        return cls(frozenset())

    def is_business_day(self, day: date) -> bool:
        # isoweekday: Monday is 1, Saturday is 6.
        return day.isoweekday() < 6 and day not in self.dates


def business_days_between(start: datetime, end: datetime, calendar: HolidayCalendar) -> int:
    """Business days in [start, end). Returns 0 when end is at or before start."""
    start_day = _local_date(start)
    end_day = _local_date(end)
    if end_day <= start_day:
        return 0

    count = 0
    cursor = start_day
    while cursor < end_day:
        if calendar.is_business_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def add_business_days(start: datetime, days: int, calendar: HolidayCalendar) -> datetime:
    """The instant `days` business days after `start`, at close of business.

    Used to stamp `review_task.sla_due_at` at assignment so the due date is fixed at the
    moment work was handed to someone, rather than recomputed later against a holiday
    calendar that may have changed.
    """
    if days < 0:
        raise ValueError("days must be non-negative")

    cursor = _local_date(start)
    remaining = days
    while remaining > 0:
        cursor += timedelta(days=1)
        if calendar.is_business_day(cursor):
            remaining -= 1

    return datetime.combine(cursor, CLOSE_OF_BUSINESS, tzinfo=DEPARTMENT_TZ)


@dataclass(frozen=True)
class PauseInterval:
    """A stretch of time the department was waiting on the applicant."""

    paused_at: datetime
    resumed_at: datetime | None = None

    def business_days(self, calendar: HolidayCalendar, as_of: datetime) -> int:
        end = self.resumed_at if self.resumed_at is not None else as_of
        return business_days_between(self.paused_at, end, calendar)


def net_business_days(
    started_at: datetime,
    as_of: datetime,
    pauses: list[PauseInterval],
    calendar: HolidayCalendar,
) -> int:
    """Business days the department has actually consumed.

    Gross elapsed minus time spent waiting on the applicant. FR-14 with FR-15.

    Clamped at zero. A pause that overlaps a weekend can in principle subtract days that
    the gross count never included if the data is inconsistent, and reporting a negative
    cycle time to the council would be worse than reporting zero.
    """
    gross = business_days_between(started_at, as_of, calendar)
    waiting = sum(p.business_days(calendar, as_of) for p in pauses)
    return max(0, gross - waiting)


@dataclass(frozen=True)
class SlaPosition:
    """Where something stands against its allowance."""

    elapsed_days: int
    allowance_days: int
    warning_threshold: float = 0.80

    @property
    def days_remaining(self) -> int:
        return self.allowance_days - self.elapsed_days

    @property
    def breached(self) -> bool:
        return self.elapsed_days > self.allowance_days

    @property
    def at_risk(self) -> bool:
        """Past the warning point but not yet breached.

        Two levels exist because a warning that fires at the same moment as the breach
        gives a supervisor no chance to reassign. See docs/02-process-map.md section 5.
        """
        return (
            not self.breached
            and self.elapsed_days >= self.allowance_days * self.warning_threshold
        )

    @property
    def state(self) -> str:
        if self.breached:
            return "BREACHED"
        if self.at_risk:
            return "AT_RISK"
        return "ON_TRACK"
