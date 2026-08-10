"""Parallel discipline review, deficiencies, and reopening (FR-08, FR-10 to FR-13)."""

from __future__ import annotations

import pytest

from permitflow.errors import GuardFailed, PermissionDenied
from permitflow.process.engine import Engine

from .conftest import APPLICANT, SUPERVISOR, reviewer_actor

DEFICIENCY = {
    "code_reference": "IRC R502.3.1",
    "description": "Floor joist span exceeds the allowable table value",
    "severity": "MAJOR",
}


class TestParallelReview:
    def test_parallel_tasks(self, conn, engine: Engine, under_review) -> None:
        """FR-08: every routed discipline is actionable at once."""
        application_id, _ = under_review("BLD-COM-NEW")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT discipline_code, status FROM review_task
                   WHERE application_id=%s ORDER BY discipline_code""",
                (str(application_id),),
            )
            tasks = cur.fetchall()
        assert {t["discipline_code"] for t in tasks} == {
            "ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL"
        }
        assert all(t["status"] == "ASSIGNED" for t in tasks)

    def test_parent_waits_for_the_last_discipline(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)
        assert engine.get_application(application_id)["status"] == "UNDER_REVIEW"

    def test_all_clean_moves_to_pending_decision(self, engine: Engine, under_review) -> None:
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            engine.approve_task(task["id"], actor)
        assert engine.get_application(application_id)["status"] == "PENDING_DECISION"


class TestReviewerScoping:
    def test_reviewer_cannot_action_another_task(self, engine: Engine, under_review) -> None:
        """NFR-01."""
        _, current_task = under_review("BLD-RES-ALT")
        zoning = reviewer_actor(current_task("ZONING"))
        structural_task = current_task("STRUCTURAL")
        with pytest.raises(PermissionDenied):
            engine.start_task(structural_task["id"], zoning)

    def test_supervisor_may_not_perform_a_review(self, engine: Engine, under_review) -> None:
        """A supervisor is not certified in every discipline, so they cannot sign off one.

        Their lever for a stuck task is reassignment, not doing the review themselves.
        """
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        with pytest.raises(PermissionDenied):
            engine.start_task(task["id"], SUPERVISOR)


class TestOutcomes:
    def test_deficiency_requires_a_code_reference(self, engine: Engine, under_review) -> None:
        """FR-19 lets a denial cite a deficiency, so an uncited one is not defensible."""
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("STRUCTURAL")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        with pytest.raises(GuardFailed):
            engine.record_deficiencies(
                task["id"], actor, [{"description": "looks wrong", "severity": "MAJOR"}]
            )

    def test_empty_deficiency_list_refused(self, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("STRUCTURAL")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        with pytest.raises(GuardFailed):
            engine.record_deficiencies(task["id"], actor, [])

    def test_conditions_required_for_conditional_approval(self, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        with pytest.raises(GuardFailed):
            engine.approve_task_with_conditions(task["id"], actor, [])

    def test_conditions_are_recorded(self, conn, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task_with_conditions(task["id"], actor, ["Provide sealed footing detail"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT description FROM review_condition WHERE review_task_id=%s",
                (str(task["id"]),),
            )
            assert cur.fetchone()["description"] == "Provide sealed footing detail"

    def test_deficiency_moves_parent_to_revisions(self, engine: Engine, under_review) -> None:
        """FR-11."""
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            if discipline == "STRUCTURAL":
                engine.record_deficiencies(task["id"], actor, [DEFICIENCY])
            else:
                engine.approve_task(task["id"], actor)
        assert engine.get_application(application_id)["status"] == "REVISIONS_REQUESTED"


class TestReopening:
    def _to_revisions(self, engine: Engine, under_review):
        application_id, current_task = under_review("BLD-RES-ALT")
        for discipline in ("ZONING", "STRUCTURAL"):
            task = current_task(discipline)
            actor = reviewer_actor(task)
            engine.start_task(task["id"], actor)
            if discipline == "STRUCTURAL":
                engine.record_deficiencies(task["id"], actor, [DEFICIENCY])
            else:
                engine.approve_task(task["id"], actor)
        return application_id, current_task

    def test_reopen_only_deficient(self, conn, engine: Engine, under_review) -> None:
        """FR-12: a discipline that already approved does not review twice."""
        application_id, _ = self._to_revisions(engine, under_review)
        engine.resubmit(application_id, APPLICANT)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT discipline_code, round, status FROM review_task
                   WHERE application_id=%s ORDER BY discipline_code, round""",
                (str(application_id),),
            )
            rows = cur.fetchall()

        round_two = [r for r in rows if r["round"] == 2]
        assert len(round_two) == 1
        assert round_two[0]["discipline_code"] == "STRUCTURAL"

    def test_round_one_history_is_retained(self, conn, engine: Engine, under_review) -> None:
        application_id, _ = self._to_revisions(engine, under_review)
        engine.resubmit(application_id, APPLICANT)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status FROM review_task
                   WHERE application_id=%s AND discipline_code='ZONING' AND round=1""",
                (str(application_id),),
            )
            assert cur.fetchone()["status"] == "APPROVED"

    def test_reopened_task_returns_to_the_same_reviewer(self, engine: Engine, under_review) -> None:
        """US-12: they wrote the deficiencies, so they already know the case."""
        application_id, current_task = self._to_revisions(engine, under_review)
        before = current_task("STRUCTURAL")["reviewer_id"]
        engine.resubmit(application_id, APPLICANT)
        after = current_task("STRUCTURAL")
        assert after["round"] == 2
        assert after["reviewer_id"] == before

    def test_approval_resolves_prior_deficiencies(self, conn, engine: Engine, under_review) -> None:
        application_id, current_task = self._to_revisions(engine, under_review)
        engine.resubmit(application_id, APPLICANT)
        task = current_task("STRUCTURAL")
        actor = reviewer_actor(task)
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS n FROM deficiency d
                   JOIN review_task rt ON rt.id = d.review_task_id
                   WHERE rt.application_id=%s AND d.resolved_at IS NULL""",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0

    def test_resubmit_from_revisions_returns_to_under_review(self, engine: Engine, under_review) -> None:
        application_id, _ = self._to_revisions(engine, under_review)
        engine.resubmit(application_id, APPLICANT)
        assert engine.get_application(application_id)["status"] == "UNDER_REVIEW"


class TestReassignment:
    def test_reassign_records_reason(self, conn, engine: Engine, under_review) -> None:
        """FR-13."""
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        engine.reassign_task(task["id"], SUPERVISOR, "reviewer on leave")

        from permitflow import audit

        rows = audit.history(conn, "review_task", task["id"])
        assert any("reviewer on leave" in str(r["after_value"]) for r in rows)

    def test_reassign_requires_a_reason(self, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        with pytest.raises(GuardFailed):
            engine.reassign_task(task["id"], SUPERVISOR, "   ")

    def test_reviewer_may_not_reassign(self, engine: Engine, under_review) -> None:
        _, current_task = under_review("BLD-RES-ALT")
        task = current_task("ZONING")
        with pytest.raises(PermissionDenied):
            engine.reassign_task(task["id"], reviewer_actor(task), "I am busy")


class TestWithdrawal:
    def test_withdraw_cancels_open_tasks(self, conn, engine: Engine, under_review) -> None:
        application_id, _ = under_review("BLD-RES-ALT")
        engine.withdraw(application_id, APPLICANT, "project cancelled")

        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS n FROM review_task
                   WHERE application_id=%s AND status NOT IN ('CANCELLED')""",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0
