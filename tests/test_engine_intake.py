"""Submission and intake screening (FR-01, FR-04, FR-05, FR-06, US-03)."""

from __future__ import annotations

import pytest

from permitflow.errors import GuardFailed, InvalidTransition, PermissionDenied
from permitflow.process.engine import Engine
from permitflow.process.states import ApplicationStatus

from .conftest import APPLICANT, CLERK, SUPERVISOR


class TestSubmit:
    def test_draft_does_not_start_the_clock(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        app = engine.get_application(application_id)
        assert app["status"] == "DRAFT"
        assert app["submitted_at"] is None

    def test_submit_starts_the_clock(self, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        app = engine.get_application(application_id)
        assert app["status"] == "SUBMITTED"
        assert app["submitted_at"] is not None

    def test_application_number_format(self, engine: Engine, make_application) -> None:
        application_id = make_application()
        number = engine.get_application(application_id)["application_number"]
        assert number.startswith("BLD-") and len(number.split("-")) == 3
        assert number.split("-")[2].isdigit() and len(number.split("-")[2]) == 5

    def test_application_numbers_are_unique(self, engine: Engine, make_application) -> None:
        numbers = {
            engine.get_application(make_application())["application_number"] for _ in range(5)
        }
        assert len(numbers) == 5

    def test_stop_work_order_blocks_submission(self, engine: Engine, make_application) -> None:
        """FR-06: no new permit on a parcel with an open stop-work order."""
        application_id = make_application(parcel_key="blocked_parcel_id")
        with pytest.raises(GuardFailed) as exc:
            engine.submit(application_id, APPLICANT)
        assert "stop-work order" in exc.value.reasons[0]
        assert engine.get_application(application_id)["status"] == "DRAFT"


class TestIllegalTransitions:
    def test_issue_from_draft_is_refused(self, engine: Engine, make_application) -> None:
        """US-03: the action does not exist on this state at all."""
        application_id = make_application()
        with pytest.raises(InvalidTransition):
            engine.issue(application_id, SUPERVISOR)
        assert engine.get_application(application_id)["status"] == "DRAFT"

    def test_refused_transition_writes_no_history(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        with pytest.raises(InvalidTransition):
            engine.issue(application_id, SUPERVISOR)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM status_history WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 0

    def test_status_and_history_never_disagree(self, conn, engine: Engine, make_application) -> None:
        """The denormalization guarantee from docs/03-data-model.md section 2."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        engine.return_incomplete(application_id, CLERK, ["missing survey"])

        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.status AS denormalized,
                          (SELECT to_status FROM status_history h
                            WHERE h.application_id = a.id
                            ORDER BY h.occurred_at DESC, h.id DESC LIMIT 1) AS latest
                   FROM application a WHERE a.id = %s""",
                (str(application_id),),
            )
            row = cur.fetchone()
        assert row["denormalized"] == row["latest"] == "RETURNED_INCOMPLETE"

    def test_applicant_may_not_accept_intake(self, engine: Engine, make_application) -> None:
        """The role check runs before the document check, so nothing leaks to the caller."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        with pytest.raises(PermissionDenied):
            engine.accept_intake(application_id, APPLICANT)


class TestIntakeScreening:
    def test_enrichment_builds_the_checklist(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        assert engine.get_application(application_id)["status"] == "INTAKE_SCREENING"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM application_document WHERE application_id=%s AND status='MISSING'",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] > 0

    def test_accept_refused_while_documents_outstanding(self, engine: Engine, make_application) -> None:
        """FR-04. Every missing document is named, not just the first."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with pytest.raises(GuardFailed) as exc:
            engine.accept_intake(application_id, CLERK)
        assert len(exc.value.reasons) >= 3
        assert all("required document not received" in r for r in exc.value.reasons)

    def test_return_requires_reasons(self, engine: Engine, make_application) -> None:
        """FR-05: a return with no itemized reason tells the applicant nothing."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with pytest.raises(GuardFailed):
            engine.return_incomplete(application_id, CLERK, [])
        assert engine.get_application(application_id)["status"] == "INTAKE_SCREENING"

    def test_return_records_every_reason(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        reasons = ["site plan not to scale", "no contractor affidavit", "survey unsealed"]
        engine.return_incomplete(application_id, CLERK, reasons)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT reason FROM status_history
                   WHERE application_id=%s AND to_status='RETURNED_INCOMPLETE'""",
                (str(application_id),),
            )
            recorded = cur.fetchone()["reason"]
        for reason in reasons:
            assert reason in recorded

    def test_resubmission_rebuilds_the_checklist(self, conn, engine: Engine, make_application) -> None:
        """A resubmission that raises the valuation can pull in a new required document."""
        application_id = make_application(valuation=40000)
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM application_document WHERE application_id=%s",
                (str(application_id),),
            )
            before = cur.fetchone()["n"]

        engine.return_incomplete(application_id, CLERK, ["valuation understated"])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE application SET declared_valuation = 200000 WHERE id=%s",
                (str(application_id),),
            )
        engine.resubmit(application_id, APPLICANT)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM application_document WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] > before

    def test_waived_document_satisfies_the_checklist(self, conn, engine: Engine, make_application) -> None:
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document
                   SET status='WAIVED', waived_by='mcarrero', waiver_reason='on file'
                   WHERE application_id=%s""",
                (str(application_id),),
            )
        engine.accept_intake(application_id, CLERK)
        assert engine.get_application(application_id)["status"] == ApplicationStatus.UNDER_REVIEW
