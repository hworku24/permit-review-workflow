"""Required document checklist derivation (FR-04)."""

from __future__ import annotations

from decimal import Decimal

from permitflow.process import checklist


class TestRequiredDocuments:
    def test_derived_from_permit_type(self, conn) -> None:
        codes = checklist.required_document_codes(conn, "BLD-RES-ACC", Decimal("15000"))
        assert set(codes) == {"SITE_PLAN", "ELEVATIONS", "CONTRACTOR_AFF"}

    def test_valuation_gate_excludes_below_threshold(self, conn) -> None:
        """A rear deck does not need sealed structural calculations."""
        codes = checklist.required_document_codes(conn, "BLD-RES-ALT", Decimal("20000"))
        assert "STRUCTURAL_CALC" not in codes
        assert "ENERGY_FORM" not in codes

    def test_valuation_gate_includes_at_threshold(self, conn) -> None:
        codes = checklist.required_document_codes(conn, "BLD-RES-ALT", Decimal("75000"))
        assert "STRUCTURAL_CALC" in codes

    def test_larger_project_pulls_in_more(self, conn) -> None:
        small = set(checklist.required_document_codes(conn, "BLD-RES-NEW", Decimal("200000")))
        large = set(checklist.required_document_codes(conn, "BLD-RES-NEW", Decimal("900000")))
        assert small < large
        assert "GEOTECH" in large and "GEOTECH" not in small

    def test_ordering_is_stable(self, conn) -> None:
        first = checklist.required_document_codes(conn, "BLD-COM-NEW", Decimal("500000"))
        second = checklist.required_document_codes(conn, "BLD-COM-NEW", Decimal("500000"))
        assert first == second == sorted(first)


class TestBuildChecklist:
    def test_creates_missing_rows(self, conn, make_application) -> None:
        application_id = make_application(permit_type_code="BLD-RES-ACC", valuation=20000)
        required = checklist.build_checklist(conn, application_id)
        assert set(checklist.missing_document_codes(conn, application_id)) == set(required)

    def test_idempotent(self, conn, make_application) -> None:
        """Runs again on every resubmission, so it must not duplicate or reset rows."""
        application_id = make_application(permit_type_code="BLD-RES-ACC", valuation=20000)
        checklist.build_checklist(conn, application_id)

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document SET status='RECEIVED', filename='a.pdf'
                   WHERE application_id=%s AND document_type_code='SITE_PLAN'""",
                (str(application_id),),
            )

        checklist.build_checklist(conn, application_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM application_document WHERE application_id=%s AND document_type_code='SITE_PLAN'",
                (str(application_id),),
            )
            assert cur.fetchone()["status"] == "RECEIVED"
            cur.execute(
                "SELECT count(*) AS n FROM application_document WHERE application_id=%s",
                (str(application_id),),
            )
            assert cur.fetchone()["n"] == 3

    def test_waived_is_not_missing(self, conn, make_application) -> None:
        application_id = make_application(permit_type_code="BLD-RES-ACC", valuation=20000)
        checklist.build_checklist(conn, application_id)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document
                   SET status='WAIVED', waived_by='mcarrero', waiver_reason='on file'
                   WHERE application_id=%s AND document_type_code='ELEVATIONS'""",
                (str(application_id),),
            )
        assert "ELEVATIONS" not in checklist.missing_document_codes(conn, application_id)

    def test_waiver_requires_owner_and_reason(self, conn, make_application) -> None:
        """Enforced by a check constraint, so a waiver is always an attributable decision."""
        import psycopg
        import pytest

        application_id = make_application(permit_type_code="BLD-RES-ACC", valuation=20000)
        checklist.build_checklist(conn, application_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE application_document SET status='WAIVED'
                       WHERE application_id=%s AND document_type_code='ELEVATIONS'""",
                    (str(application_id),),
                )
