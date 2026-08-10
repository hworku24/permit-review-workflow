"""Issuance, denial, and appeal (FR-18, FR-19)."""

from __future__ import annotations

import pytest

from permitflow.errors import GuardFailed, InvalidTransition, PermissionDenied
from permitflow.process.engine import Engine

from .conftest import APPLICANT, CLERK, SUPERVISOR, reviewer_actor


def _approve_all(engine: Engine, current_task, disciplines) -> None:
    for discipline in disciplines:
        task = current_task(discipline)
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)


class TestIssue:
    def test_issue_requires_all_terminal(self, engine: Engine, under_review) -> None:
        """FR-18: no permit is issued over an unreviewed discipline."""
        application_id, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)

        with pytest.raises(InvalidTransition):
            engine.issue(application_id, SUPERVISOR)

    def test_issue_blocked_from_pending_decision_with_open_task(
        self, conn, engine: Engine, under_review
    ) -> None:
        """The guard, not just the transition table, catches a task reopened after the fact."""
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        assert engine.get_application(application_id)["status"] == "PENDING_DECISION"

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE review_task SET status='IN_PROGRESS', completed_at=NULL
                   WHERE application_id=%s AND discipline_code='ZONING'""",
                (str(application_id),),
            )
        with pytest.raises(GuardFailed) as exc:
            engine.issue(application_id, SUPERVISOR)
        assert "ZONING" in exc.value.reasons[0]

    def test_supervisor_issues(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        engine.issue(application_id, SUPERVISOR)

        app = engine.get_application(application_id)
        assert app["status"] == "ISSUED"
        assert app["decided_at"] is not None

    def test_clerk_may_not_issue(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        with pytest.raises(PermissionDenied):
            engine.issue(application_id, CLERK)

    def test_conditional_approval_counts_as_signed_off(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            engine.approve_task_with_conditions(task["id"], actor, ["submit revised detail"])
        engine.issue(application_id, SUPERVISOR)
        assert engine.get_application(application_id)["status"] == "ISSUED"


class TestDeny:
    def test_deny_requires_reason(self, engine: Engine, under_review) -> None:
        """FR-19."""
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        with pytest.raises(GuardFailed):
            engine.deny(application_id, SUPERVISOR, "   ")

    def test_deny_records_reason(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        engine.deny(application_id, SUPERVISOR, "Lot coverage exceeds § 59-2.2.4 maximum")

        with conn.cursor() as cur:
            cur.execute(
                """SELECT reason FROM status_history
                   WHERE application_id=%s AND to_status='DENIED'""",
                (str(application_id),),
            )
            assert "59-2.2.4" in cur.fetchone()["reason"]


class TestAppeal:
    def test_appeal_freezes_a_denied_case(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        engine.deny(application_id, SUPERVISOR, "does not meet setback")
        engine.file_appeal(application_id, APPLICANT, "requesting a variance hearing")
        assert engine.get_application(application_id)["status"] == "APPEAL_FILED"

    def test_appeal_only_from_denied(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        _approve_all(engine, current_task, ("ZONING", "STRUCTURAL"))
        engine.issue(application_id, SUPERVISOR)
        with pytest.raises(InvalidTransition):
            engine.file_appeal(application_id, APPLICANT, "no")
