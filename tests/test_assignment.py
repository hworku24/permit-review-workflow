"""Reviewer assignment (FR-09, US-09).

Discovery pain point P4: two of seven reviewers carried 44% of Q1 volume, because
assignment happened in a weekly meeting and nobody could see the distribution.
"""

from __future__ import annotations

from permitflow.process import assignment
from permitflow.process.engine import Engine

INACTIVE_REVIEWER = "tbrandt"


class TestEligibility:
    def test_only_certified_reviewers(self, conn) -> None:
        usernames = {c.username for c in assignment.eligible_reviewers(conn, "FIRE")}
        assert usernames == {"abassett", "nmalik"}

    def test_inactive_reviewers_excluded(self, conn) -> None:
        """Tomas is certified in zoning and structural but is not active."""
        for discipline in ("ZONING", "STRUCTURAL"):
            usernames = {c.username for c in assignment.eligible_reviewers(conn, discipline)}
            assert INACTIVE_REVIEWER not in usernames

    def test_clerks_and_supervisors_are_not_reviewers(self, conn) -> None:
        for discipline in ("ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL"):
            usernames = {c.username for c in assignment.eligible_reviewers(conn, discipline)}
            assert not usernames & {"mcarrero", "jtakeda", "dhollis"}

    def test_unknown_discipline_has_nobody(self, conn) -> None:
        assert assignment.eligible_reviewers(conn, "HISTORIC") == []


class TestPickReviewer:
    def test_picks_least_loaded(self, conn) -> None:
        candidate = assignment.pick_reviewer(conn, "FIRE")
        loads = [c.open_tasks for c in assignment.eligible_reviewers(conn, "FIRE")]
        assert candidate.open_tasks == min(loads)

    def test_tie_breaks_deterministically(self, conn) -> None:
        """Same input assigns the same way, so a failing assignment test is reproducible."""
        picks = {assignment.pick_reviewer(conn, "FIRE").username for _ in range(5)}
        assert len(picks) == 1

    def test_returns_none_when_nobody_eligible(self, conn) -> None:
        assert assignment.pick_reviewer(conn, "HISTORIC") is None


class TestAssignmentInPractice:
    def test_load_spreads_across_reviewers(self, conn, engine: Engine, under_review) -> None:
        """Two commercial applications should not both land on the same fire reviewer.

        This is discovery pain point P4 stated as a test. Counting the fire tasks per
        reviewer rather than each reviewer's total queue, because the fire reviewers are
        also certified elsewhere and pick up structural work in the same fixture.
        """
        under_review("BLD-COM-NEW")
        under_review("BLD-COM-NEW")

        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.username, count(*) AS n
                   FROM review_task rt JOIN reviewer r ON r.id = rt.reviewer_id
                   WHERE rt.discipline_code = 'FIRE'
                   GROUP BY r.username"""
            )
            per_reviewer = {row["username"]: row["n"] for row in cur.fetchall()}

        assert sum(per_reviewer.values()) == 2
        assert len(per_reviewer) == 2, f"both fire reviewers should have one each: {per_reviewer}"

    def test_unassignable_task_stays_pending_and_escalates(
        self, conn, engine: Engine, make_application
    ) -> None:
        """US-09: an uncertified review is a worse outcome than a late one."""
        from .conftest import APPLICANT, CLERK

        with conn.cursor() as cur:
            cur.execute("UPDATE reviewer SET active = false WHERE role = 'reviewer'")

        application_id = make_application(permit_type_code="BLD-RES-ACC", valuation=15000)
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document SET status='RECEIVED', filename='x.pdf',
                       uploaded_at=now(), uploaded_by='t'
                   WHERE application_id=%s""",
                (str(application_id),),
            )
        engine.accept_intake(application_id, CLERK)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, reviewer_id FROM review_task WHERE application_id=%s",
                (str(application_id),),
            )
            task = cur.fetchone()
            assert task["status"] == "PENDING"
            assert task["reviewer_id"] is None

            cur.execute(
                "SELECT level, reason FROM escalation WHERE application_id=%s",
                (str(application_id),),
            )
            escalation = cur.fetchone()
            assert escalation is not None
            assert "no active certified reviewer" in escalation["reason"]

    def test_reassignment_moves_the_task(self, conn, engine: Engine, under_review) -> None:
        from .conftest import SUPERVISOR

        _, current_task = under_review("BLD-COM-NEW")
        before = current_task("FIRE")
        engine.reassign_task(before["id"], SUPERVISOR, "reviewer on leave")
        after = current_task("FIRE")
        assert after["reviewer_id"] != before["reviewer_id"]
        assert after["status"] == "ASSIGNED"
