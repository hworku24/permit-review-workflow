"""SLA escalation sweep (FR-16, FR-17).

Discovery pain point P3: work sat in queues nobody was watching for a median of six days.
The warning level is the part that changes an outcome, because a supervisor who only hears
about a breach at the moment it happens has already lost the chance to act.
"""

from __future__ import annotations

from datetime import timedelta

from permitflow.process import escalation
from permitflow.process.engine import Engine


def _backdate_task(conn, application_id, discipline: str, business_days: int) -> None:
    """Move a task's assignment back far enough to cross a threshold.

    Calendar days rather than business days, deliberately over-shooting, because the view
    computes business days itself and the test is about the escalation levels rather than
    about the date arithmetic. That is covered in test_sla_parity.py.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE review_task SET assigned_at = now() - %s::interval
               WHERE application_id = %s AND discipline_code = %s""",
            (timedelta(days=business_days * 7 / 5 + 2), str(application_id), discipline),
        )


class TestSweep:
    def test_no_escalations_when_on_track(self, conn, under_review) -> None:
        under_review("BLD-RES-ALT")
        result = escalation.sweep(conn)
        assert result.total == 0

    def test_warning_at_eighty_percent(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 12)  # allowance is 14

        result = escalation.sweep(conn)
        assert result.warnings_raised == 1
        assert result.breaches_raised == 0

        with conn.cursor() as cur:
            cur.execute(
                "SELECT level, reason FROM escalation WHERE application_id=%s",
                (str(application_id),),
            )
            row = cur.fetchone()
        assert row["level"] == "WARNING"
        assert "ZONING" in row["reason"]

    def test_breach_past_allowance(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 20)

        result = escalation.sweep(conn)
        assert result.breaches_raised == 1

    def test_no_duplicate_at_the_same_level(self, conn, under_review) -> None:
        """A sweep that runs every five minutes must not produce 288 rows a day."""
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 20)

        first = escalation.sweep(conn)
        second = escalation.sweep(conn)
        third = escalation.sweep(conn)

        assert first.total == 1
        assert second.total == 0 and third.total == 0

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM escalation WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 1

    def test_warning_and_breach_are_separate_rows(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 12)
        escalation.sweep(conn)
        _backdate_task(conn, application_id, "ZONING", 20)
        escalation.sweep(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT level FROM escalation WHERE application_id=%s ORDER BY level",
                (str(application_id),),
            )
            assert [r["level"] for r in cur.fetchall()] == ["BREACH", "WARNING"]

    def test_closed_tasks_do_not_escalate(self, conn, engine: Engine, under_review) -> None:
        from .conftest import reviewer_actor

        application_id, current_task = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 20)
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)

        assert escalation.sweep(conn).total == 0


class TestAcknowledge:
    def test_acknowledge_removes_from_the_open_queue(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 20)
        escalation.sweep(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT escalation_id FROM v_open_escalations")
            escalation_id = cur.fetchone()["escalation_id"]

        escalation.acknowledge(conn, escalation_id, "dhollis")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM v_open_escalations")
            assert cur.fetchone()["n"] == 0
            cur.execute(
                "SELECT acknowledged_by FROM escalation WHERE id=%s", (str(escalation_id),)
            )
            assert cur.fetchone()["acknowledged_by"] == "dhollis"

    def test_acknowledging_twice_is_harmless(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        _backdate_task(conn, application_id, "ZONING", 20)
        escalation.sweep(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT escalation_id FROM v_open_escalations")
            escalation_id = cur.fetchone()["escalation_id"]

        escalation.acknowledge(conn, escalation_id, "dhollis")
        escalation.acknowledge(conn, escalation_id, "jtakeda")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT acknowledged_by FROM escalation WHERE id=%s", (str(escalation_id),)
            )
            assert cur.fetchone()["acknowledged_by"] == "dhollis"
