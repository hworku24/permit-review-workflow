"""Business day math and clock pauses (FR-14, FR-15, NFR-05).

NFR-05 calls this the most tested logic in the system because the compliance figure it
produces is reported to the city council. A cycle time that quietly counts weekends is a
public number that is wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from permitflow.process.engine import Engine
from permitflow.process.sla import (
    HolidayCalendar,
    PauseInterval,
    SlaPosition,
    add_business_days,
    business_days_between,
    net_business_days,
)

from .conftest import APPLICANT, CLERK


def dt(y: int, m: int, d: int, hour: int = 9) -> datetime:
    return datetime(y, m, d, hour, tzinfo=UTC)


@pytest.fixture
def cal() -> HolidayCalendar:
    return HolidayCalendar.from_iterable(
        {date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 11, 27)}
    )


class TestBusinessDaysBetween:
    def test_same_instant_is_zero(self, cal: HolidayCalendar) -> None:
        assert business_days_between(dt(2026, 8, 3), dt(2026, 8, 3), cal) == 0

    def test_same_day_later_hour_is_zero(self, cal: HolidayCalendar) -> None:
        # Half-open interval: the day of submission is not charged twice.
        assert business_days_between(dt(2026, 8, 3, 9), dt(2026, 8, 3, 17), cal) == 0

    def test_plain_week_counts_five(self, cal: HolidayCalendar) -> None:
        # Monday Aug 3 to Monday Aug 10.
        assert business_days_between(dt(2026, 8, 3), dt(2026, 8, 10), cal) == 5

    def test_weekend_does_not_count(self, cal: HolidayCalendar) -> None:
        # Friday Aug 7 to Monday Aug 10 is one business day, not three.
        assert business_days_between(dt(2026, 8, 7), dt(2026, 8, 10), cal) == 1

    def test_holiday_does_not_count(self, cal: HolidayCalendar) -> None:
        # Wed Jul 1 to Wed Jul 8 spans Jul 3 (observed holiday) and a weekend.
        assert business_days_between(dt(2026, 7, 1), dt(2026, 7, 8), cal) == 4

    def test_consecutive_holidays(self, cal: HolidayCalendar) -> None:
        # Thanksgiving Thu Nov 26 and the Friday after are both holidays.
        assert business_days_between(dt(2026, 11, 25), dt(2026, 11, 30), cal) == 1

    def test_end_before_start_is_zero(self, cal: HolidayCalendar) -> None:
        assert business_days_between(dt(2026, 8, 10), dt(2026, 8, 3), cal) == 0


class TestAddBusinessDays:
    def test_skips_weekend(self, cal: HolidayCalendar) -> None:
        # Friday plus one business day lands on Monday.
        assert add_business_days(dt(2026, 8, 7), 1, cal).date() == date(2026, 8, 10)

    def test_skips_holiday(self, cal: HolidayCalendar) -> None:
        # Wed Jul 1 plus 3: Thu Jul 2, skip Fri Jul 3 and the weekend, Mon Jul 6, Tue Jul 7.
        assert add_business_days(dt(2026, 7, 1), 3, cal).date() == date(2026, 7, 7)

    def test_lands_at_close_of_business(self, cal: HolidayCalendar) -> None:
        assert add_business_days(dt(2026, 8, 3), 1, cal).hour == 17

    def test_zero_days_is_same_day(self, cal: HolidayCalendar) -> None:
        assert add_business_days(dt(2026, 8, 3), 0, cal).date() == date(2026, 8, 3)

    def test_negative_rejected(self, cal: HolidayCalendar) -> None:
        with pytest.raises(ValueError):
            add_business_days(dt(2026, 8, 3), -1, cal)


class TestNetBusinessDays:
    def test_pause_excluded(self, cal: HolidayCalendar) -> None:
        """Time waiting on the applicant is not charged to the department (FR-15)."""
        pauses = [PauseInterval(dt(2026, 8, 5), dt(2026, 8, 12))]  # Wed to Wed, 5 business days
        gross = business_days_between(dt(2026, 8, 3), dt(2026, 8, 17), cal)
        net = net_business_days(dt(2026, 8, 3), dt(2026, 8, 17), pauses, cal)
        assert gross == 10
        assert net == 5

    def test_open_pause_runs_to_now(self, cal: HolidayCalendar) -> None:
        pauses = [PauseInterval(dt(2026, 8, 5), None)]
        assert net_business_days(dt(2026, 8, 3), dt(2026, 8, 12), pauses, cal) == 2

    def test_never_negative(self, cal: HolidayCalendar) -> None:
        # An inconsistent pause must not produce a negative cycle time in a council report.
        pauses = [PauseInterval(dt(2026, 7, 1), dt(2026, 12, 1))]
        assert net_business_days(dt(2026, 8, 3), dt(2026, 8, 10), pauses, cal) == 0


class TestSlaPosition:
    def test_on_track(self) -> None:
        p = SlaPosition(elapsed_days=5, allowance_days=14)
        assert p.state == "ON_TRACK" and p.days_remaining == 9

    def test_at_risk_at_eighty_percent(self) -> None:
        assert SlaPosition(elapsed_days=12, allowance_days=14).state == "AT_RISK"

    def test_breached_strictly_over(self) -> None:
        assert SlaPosition(elapsed_days=14, allowance_days=14).breached is False
        assert SlaPosition(elapsed_days=15, allowance_days=14).breached is True


class TestClockPausesInTheEngine:
    def test_pause_on_returned_incomplete(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["site plan not to scale"])

        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, resumed_at FROM clock_pause WHERE application_id=%s",
                (str(application_id),),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["reason"] == "RETURNED_INCOMPLETE"
        assert rows[0]["resumed_at"] is None

    def test_resubmit_closes_the_pause(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["missing affidavit"])
        engine.resubmit(application_id, APPLICANT)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM clock_pause WHERE application_id=%s AND resumed_at IS NULL",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0

    def test_pause_on_revisions(self, conn, engine: Engine, under_review) -> None:
        """Deficiencies pause the clock while the applicant corrects them (FR-11)."""
        from .conftest import reviewer_actor

        application_id, current_task = under_review()
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            if discipline == "STRUCTURAL":
                engine.record_deficiencies(
                    task["id"], actor,
                    [{"code_reference": "IRC R502.3.1", "description": "span exceeds table",
                      "severity": "MAJOR"}],
                )
            else:
                engine.approve_task(task["id"], actor)

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM application WHERE id=%s", (str(application_id),))
            assert cur.fetchone()["status"] == "REVISIONS_REQUESTED"
            cur.execute(
                "SELECT reason FROM clock_pause WHERE application_id=%s AND resumed_at IS NULL",
                (str(application_id),),
            )
            assert cur.fetchone()["reason"] == "REVISIONS_REQUESTED"

    def test_terminal_state_closes_an_open_pause(self, conn, engine: Engine, make_application) -> None:
        """A case withdrawn while waiting on the applicant must not stay paused forever."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["incomplete"])
        engine.withdraw(application_id, APPLICANT, "applicant changed plans")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM clock_pause WHERE application_id=%s AND resumed_at IS NULL",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0

    def test_only_one_open_pause_at_a_time(self, conn, engine: Engine, make_application) -> None:
        """Enforced by a partial unique index rather than by application code."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["incomplete"])

        import psycopg

        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clock_pause (application_id, paused_at, reason) VALUES (%s, now(), 'x')",
                    (str(application_id),),
                )
