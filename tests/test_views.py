"""Reporting views (FR-17, FR-22, FR-23, FR-24).

The compliance figure goes to the city council, so these are tested against real rows
rather than assumed correct because the SQL reads plausibly.
"""

from __future__ import annotations

from permitflow.process import escalation
from permitflow.process.engine import Engine

from .conftest import APPLICANT, SUPERVISOR, reviewer_actor


def _decide(engine: Engine, application_id, current_task, disciplines) -> None:
    for discipline in disciplines:
        task = current_task(discipline)
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)
    engine.issue(application_id, SUPERVISOR)


class TestApplicationSummary:
    def test_waiting_on_tracks_state(self, conn, engine: Engine, make_application) -> None:
        from .conftest import CLERK

        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT waiting_on, clock_paused FROM v_application_summary WHERE application_id=%s",
                (str(application_id),),
            )
            row = cur.fetchone()
        assert row["waiting_on"] == "intake clerk" and row["clock_paused"] is False

        engine.return_incomplete(application_id, CLERK, ["missing survey"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT waiting_on, clock_paused FROM v_application_summary WHERE application_id=%s",
                (str(application_id),),
            )
            row = cur.fetchone()
        assert row["waiting_on"] == "applicant" and row["clock_paused"] is True

    def test_counts_reflect_open_work(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-COM-NEW")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT open_task_count, missing_document_count FROM v_application_summary WHERE application_id=%s",
                (str(application_id),),
            )
            row = cur.fetchone()
        assert row["open_task_count"] == 4
        assert row["missing_document_count"] == 0


class TestSlaStatus:
    def test_not_started_before_submission(self, conn, make_application) -> None:
        application_id = make_application()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sla_state FROM v_sla_status WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["sla_state"] == "NOT_STARTED"

    def test_allowance_comes_from_the_permit_type(self, conn, under_review) -> None:
        residential, _ = under_review("BLD-RES-ALT")
        commercial, _ = under_review("BLD-COM-NEW")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT application_id, allowance_days FROM v_sla_status WHERE application_id = ANY(%s)",
                ([str(residential), str(commercial)],),
            )
            allowances = {str(r["application_id"]): r["allowance_days"] for r in cur.fetchall()}
        assert allowances[str(residential)] == 20
        assert allowances[str(commercial)] == 30

    def test_met_when_decided_inside_the_allowance(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _decide(engine, application_id, current_task, ("ZONING", "STRUCTURAL"))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sla_state FROM v_sla_status WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["sla_state"] == "MET"


class TestCycleTime:
    def test_cycle_time(self, conn, engine: Engine, under_review) -> None:
        """FR-22. Gross minus applicant wait equals net."""
        application_id, current_task = under_review("BLD-RES-ALT")
        _decide(engine, application_id, current_task, ("ZONING", "STRUCTURAL"))

        with conn.cursor() as cur:
            cur.execute(
                """SELECT gross_business_days, net_business_days,
                          applicant_wait_business_days, review_rounds
                   FROM v_cycle_time WHERE application_id=%s""",
                (str(application_id),),
            )
            row = cur.fetchone()

        assert row["net_business_days"] == row["gross_business_days"] - row["applicant_wait_business_days"]
        assert row["review_rounds"] == 1

    def test_applicant_wait_is_excluded(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")

        # Backdate submission and record a closed pause covering most of the elapsed time.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE application SET submitted_at = now() - interval '30 days' WHERE id=%s",
                (str(application_id),),
            )
            cur.execute(
                """INSERT INTO clock_pause (application_id, paused_at, resumed_at, reason)
                   VALUES (%s, now() - interval '25 days', now() - interval '5 days', 'REVISIONS_REQUESTED')""",
                (str(application_id),),
            )

        _decide(engine, application_id, current_task, ("ZONING", "STRUCTURAL"))

        with conn.cursor() as cur:
            cur.execute(
                """SELECT gross_business_days, net_business_days, applicant_wait_business_days
                   FROM v_cycle_time WHERE application_id=%s""",
                (str(application_id),),
            )
            row = cur.fetchone()

        assert row["applicant_wait_business_days"] > 10
        assert row["net_business_days"] < row["gross_business_days"]
        assert row["net_business_days"] == row["gross_business_days"] - row["applicant_wait_business_days"]

    def test_undecided_applications_are_excluded(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM v_cycle_time WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0


class TestSlaCompliance:
    def test_sla_compliance(self, conn, engine: Engine, under_review) -> None:
        """FR-23: the number reported to council."""
        for _ in range(3):
            application_id, current_task = under_review("BLD-RES-ALT")
            _decide(engine, application_id, current_task, ("ZONING", "STRUCTURAL"))

        with conn.cursor() as cur:
            cur.execute(
                """SELECT decided_count, met_count, compliance_pct, median_net_days, p90_net_days
                   FROM v_sla_compliance WHERE permit_type_code='BLD-RES-ALT'"""
            )
            row = cur.fetchone()

        assert row["decided_count"] == 3
        assert row["met_count"] == 3
        assert float(row["compliance_pct"]) == 100.0
        assert row["median_net_days"] is not None and row["p90_net_days"] is not None

    def test_breached_application_lowers_the_rate(self, conn, engine: Engine, under_review) -> None:
        for index in range(2):
            application_id, current_task = under_review("BLD-RES-ALT")
            if index == 0:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE application SET submitted_at = now() - interval '60 days' WHERE id=%s",
                        (str(application_id),),
                    )
            _decide(engine, application_id, current_task, ("ZONING", "STRUCTURAL"))

        with conn.cursor() as cur:
            cur.execute(
                """SELECT decided_count, met_count, compliance_pct
                   FROM v_sla_compliance WHERE permit_type_code='BLD-RES-ALT'"""
            )
            row = cur.fetchone()

        assert row["decided_count"] == 2
        assert row["met_count"] == 1
        assert float(row["compliance_pct"]) == 50.0


class TestReviewerWorkload:
    def test_reviewer_workload(self, conn, under_review) -> None:
        """FR-24."""
        under_review("BLD-COM-NEW")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT username, discipline_code, open_tasks
                   FROM v_reviewer_workload WHERE open_tasks > 0"""
            )
            rows = cur.fetchall()
        assert {r["discipline_code"] for r in rows} == {
            "ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL"
        }

    def test_inactive_reviewers_are_still_listed(self, conn) -> None:
        """A supervisor needs to see them to know why coverage is thin."""
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM v_reviewer_workload WHERE NOT active")
            assert cur.fetchone()["n"] > 0


class TestOpenEscalations:
    def test_open_escalations_ordering(self, conn, under_review) -> None:
        """FR-17: breaches first, then by how far past due."""
        far_past_due, _ = under_review("BLD-RES-ALT")
        near_due, _ = under_review("BLD-RES-ALT")

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE review_task SET assigned_at = now() - interval '60 days'
                   WHERE application_id=%s AND discipline_code='ZONING'""",
                (str(far_past_due),),
            )
            cur.execute(
                """UPDATE review_task SET assigned_at = now() - interval '18 days'
                   WHERE application_id=%s AND discipline_code='ZONING'""",
                (str(near_due),),
            )
        escalation.sweep(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT level, business_days_past_due FROM v_open_escalations")
            rows = cur.fetchall()

        assert len(rows) >= 2
        breaches = [r for r in rows if r["level"] == "BREACH"]
        assert rows[0]["level"] == "BREACH"
        past_due = [r["business_days_past_due"] for r in breaches]
        assert past_due == sorted(past_due, reverse=True)

    def test_acknowledged_escalations_are_hidden(self, conn, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE review_task SET assigned_at = now() - interval '60 days'
                   WHERE application_id=%s""",
                (str(application_id),),
            )
        escalation.sweep(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT escalation_id FROM v_open_escalations LIMIT 1")
            escalation_id = cur.fetchone()["escalation_id"]
        escalation.acknowledge(conn, escalation_id, "dhollis")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM v_open_escalations WHERE escalation_id=%s",
                (str(escalation_id),),
            )
            assert cur.fetchone()["n"] == 0


class TestDisciplineBottleneck:
    """v_discipline_bottleneck: where cases sit, as opposed to who is loaded."""

    def test_every_configured_discipline_appears_even_with_no_work(self, conn) -> None:
        """A discipline missing from the list reads as zero, and zero reads as measured."""
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM discipline")
            configured = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM v_discipline_bottleneck")
            listed = cur.fetchone()["n"]
        assert listed == configured

    def test_open_work_is_counted_against_its_discipline(self, conn, under_review) -> None:
        application_id, current_task = under_review()
        task = current_task("ZONING")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT open_tasks FROM v_discipline_bottleneck WHERE discipline_code = 'ZONING'"
            )
            assert cur.fetchone()["open_tasks"] >= 1
            cur.execute(
                "SELECT open_tasks FROM v_discipline_bottleneck WHERE discipline_code = 'FIRE'"
            )
            # BLD-RES-ALT does not route to fire, so it stays at zero rather than absent.
            assert cur.fetchone()["open_tasks"] == 0
        assert task is not None

    def test_an_approved_task_stops_counting_as_open(
        self, conn, engine: Engine, under_review
    ) -> None:
        application_id, current_task = under_review()
        task = current_task("ZONING")
        engine.start_task(task["id"], reviewer_actor(task))
        engine.approve_task(task["id"], reviewer_actor(task))

        with conn.cursor() as cur:
            cur.execute(
                """SELECT open_tasks, completed_last_30_days
                   FROM v_discipline_bottleneck WHERE discipline_code = 'ZONING'"""
            )
            row = cur.fetchone()
        assert row["open_tasks"] == 0
        assert row["completed_last_30_days"] == 1

    def test_a_breached_task_is_counted_as_overdue(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review()
        task = current_task("ZONING")

        # Age the assignment past the allowance. The view reads the clock, so moving the
        # assignment backwards is the same thing as time passing.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE review_task SET assigned_at = now() - interval '200 days' WHERE id = %s",
                (str(task["id"]),),
            )
            cur.execute(
                "SELECT breached_tasks FROM v_discipline_bottleneck WHERE discipline_code = 'ZONING'"
            )
            assert cur.fetchone()["breached_tasks"] == 1
