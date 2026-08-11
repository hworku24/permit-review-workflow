"""Audit trail (FR-20, FR-21, NFR-04).

Discovery pain point P6: when the 2025 Aldergate denial was appealed, nobody could
reconstruct who changed the zoning determination or when.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from permitflow import audit
from permitflow.process.engine import Engine

from .conftest import APPLICANT, CLERK, SUPERVISOR, reviewer_actor


class TestAppendOnly:
    def test_audit_is_append_only(self, conn) -> None:
        """FR-21, enforced by triggers rather than by convention."""
        audit.record(
            conn, entity_type="application", entity_id=None, action="test", actor="tester"
        )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("UPDATE audit_log SET actor = 'someone-else'")

    def test_delete_is_rejected(self, conn) -> None:
        audit.record(
            conn, entity_type="application", entity_id=None, action="test", actor="tester"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log")

    def test_truncate_is_rejected(self, conn) -> None:
        """TRUNCATE bypasses row triggers, so it needs its own statement-level guard."""
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("TRUNCATE audit_log")


class TestTransitionAuditing:
    def test_transition_writes_audit(self, conn, engine: Engine, make_application) -> None:
        """FR-20: actor, timestamp, before, and after."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)

        rows = audit.history(conn, "application", application_id)
        transition = [r for r in rows if r["action"] == "transition:submit"]
        assert len(transition) == 1

        entry = transition[0]
        assert entry["actor"] == APPLICANT.username
        assert entry["before_value"]["status"] == "DRAFT"
        assert entry["after_value"]["status"] == "SUBMITTED"
        assert entry["occurred_at"] is not None

    def test_every_transition_is_recorded(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            engine.approve_task(task["id"], actor)
        engine.issue(application_id, SUPERVISOR)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM status_history WHERE application_id=%s",
                (str(application_id),),
            )
            transitions = cur.fetchone()["n"]

        audited = [
            r
            for r in audit.history(conn, "application", application_id)
            if r["action"].startswith("transition:")
        ]
        assert len(audited) == transitions

    def test_history_is_ordered(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["missing survey"])

        rows = audit.history(conn, "application", application_id)
        timestamps = [r["occurred_at"] for r in rows]
        assert timestamps == sorted(timestamps)
        assert rows[0]["action"] == "create"

    def test_denial_reason_is_in_the_audit(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            engine.approve_task(task["id"], actor)
        engine.deny(application_id, SUPERVISOR, "Rear setback below § 59-2.2.4 minimum")

        rows = audit.history(conn, "application", application_id)
        denial = [r for r in rows if r["action"] == "transition:deny"][0]
        assert "59-2.2.4" in denial["after_value"]["reason"]
        assert denial["actor"] == SUPERVISOR.username

    def test_task_actions_are_audited(self, conn, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)

        actions = {r["action"] for r in audit.history(conn, "review_task", task["id"])}
        assert "open" in actions
        assert "transition:assign" in actions
        assert "transition:approve" in actions

    def test_a_case_can_be_reconstructed(self, conn, engine: Engine, under_review) -> None:
        """US-16: an appeal is answered with facts, not recollection."""
        application_id, current_task = under_review("BLD-RES-ALT")
        task = current_task("STRUCTURAL")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.record_deficiencies(
            task["id"],
            actor,
            [{"code_reference": "IRC R502.3.1", "description": "span exceeds table", "severity": "MAJOR"}],
        )

        rows = audit.history(conn, "review_task", task["id"])
        assert any(r["actor"] == actor.username for r in rows)
        assert all(r["occurred_at"] is not None for r in rows)


class TestAuditTimestampsFollowTheEngineClock:
    """FR-20: an audit row records when the action happened, not when the row was written.

    The two are the same in production and differ in any seeded or backfilled history. A
    trail that disagrees with the status history it describes is the one defect an audit
    trail cannot have, since reconstructing a sequence is the whole reason it exists.
    """

    def test_a_transition_is_stamped_with_the_clock_that_moved_it(
        self, conn, make_application, clock
    ) -> None:
        clock.now = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        engine = Engine(conn, clock=clock)
        application_id = make_application()
        engine.submit(application_id, APPLICANT)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT occurred_at FROM audit_log
                   WHERE entity_id = %s AND action = 'transition:submit'""",
                (str(application_id),),
            )
            audited = cur.fetchone()["occurred_at"]

        assert audited == clock.now

    def test_the_audit_row_and_the_status_row_agree(self, conn, make_application, clock) -> None:
        clock.now = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        engine = Engine(conn, clock=clock)
        application_id = make_application()
        engine.submit(application_id, APPLICANT)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT sh.occurred_at AS status_at, al.occurred_at AS audit_at
                   FROM status_history sh
                   JOIN audit_log al ON al.entity_id = sh.application_id
                                    AND al.action = 'transition:' || 'submit'
                   WHERE sh.application_id = %s AND sh.to_status = 'SUBMITTED'""",
                (str(application_id),),
            )
            row = cur.fetchone()

        assert row["status_at"] == row["audit_at"]
