"""End-to-end API contract tests.

These drive the whole case through HTTP, which is the only place the error mapping, the
integration wiring, and the engine are exercised together.
"""

from __future__ import annotations

import pytest

from permitflow.db import read_connection


def hdr(username: str) -> dict[str, str]:
    return {"X-Actor": username}


APPLICANT = "priya"
CLERK = "mcarrero"
SUPERVISOR = "dhollis"

NARRATIVE = (
    "New single family home on a wooded lot. Clearing and grading, adds 6,200 sq ft of "
    "impervious area and a stormwater facility. Valuation $720,000."
)


@pytest.fixture
def application(api_client, committed_parties) -> str:
    response = api_client.post(
        "/applications",
        headers=hdr(APPLICANT),
        json={
            "applicant_id": str(committed_parties["applicant_id"]),
            "parcel_id": str(committed_parties["parcel_id"]),
            "contractor_id": str(committed_parties["contractor_id"]),
            "permit_type_code": "BLD-RES-NEW",
            "scope_narrative": NARRATIVE,
            "declared_valuation": "720000",
            "square_feet": 3100,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["application_id"]


def _satisfy_documents(api_client, application_id: str) -> None:
    outstanding = api_client.post(
        f"/applications/{application_id}/submit", headers=hdr(APPLICANT)
    ).json()["required_documents"]
    for code in outstanding:
        response = api_client.put(
            f"/applications/{application_id}/documents/{code}",
            headers=hdr(APPLICANT),
            json={"filename": f"{code.lower()}.pdf"},
        )
        assert response.status_code == 200, response.text


def _current_task(application_id: str, discipline: str) -> dict:
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT rt.id, r.username FROM review_task rt
               JOIN reviewer r ON r.id = rt.reviewer_id
               WHERE rt.application_id=%s AND rt.discipline_code=%s
               ORDER BY rt.round DESC LIMIT 1""",
            (application_id, discipline),
        )
        return cur.fetchone()


class TestSubmission:
    def test_submit_creates_application(self, api_client, application) -> None:
        """FR-01."""
        response = api_client.post(
            f"/applications/{application}/submit", headers=hdr(APPLICANT)
        )
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "INTAKE_SCREENING"
        assert body["parcel_verified"] is True
        assert body["required_documents"]

    def test_submit_enriches_from_the_county(self, api_client, application) -> None:
        """FR-02."""
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        view = api_client.get(f"/applications/{application}", headers=hdr(CLERK)).json()
        assert view["zoning_code"] == "R-90"
        assert view["parcel_verified"] is True

    def test_submit_verifies_the_licence(self, api_client, application) -> None:
        """FR-03."""
        body = api_client.post(
            f"/applications/{application}/submit", headers=hdr(APPLICANT)
        ).json()
        assert "active" in body["license_note"]

    def test_stop_work_order_blocks_submission(
        self, api_client, committed_parties
    ) -> None:
        """FR-06 surfaced as a 422 with the reason named."""
        created = api_client.post(
            "/applications",
            headers=hdr(APPLICANT),
            json={
                "applicant_id": str(committed_parties["applicant_id"]),
                "parcel_id": str(committed_parties["blocked_parcel_id"]),
                "permit_type_code": "BLD-RES-ALT",
                "scope_narrative": "Rear addition of 400 square feet.",
                "declared_valuation": "80000",
            },
        ).json()

        response = api_client.post(
            f"/applications/{created['application_id']}/submit", headers=hdr(APPLICANT)
        )
        assert response.status_code == 422
        assert "stop-work order" in response.json()["reasons"][0]

    def test_county_outage_does_not_block_intake(
        self, api_client, application, soap_failures
    ) -> None:
        """NFR-02."""
        soap_failures.outage = True
        response = api_client.post(
            f"/applications/{application}/submit", headers=hdr(APPLICANT)
        )
        assert response.status_code == 200
        assert response.json()["parcel_verified"] is False

        view = api_client.get(f"/applications/{application}", headers=hdr(CLERK)).json()
        assert view["status"] == "INTAKE_SCREENING"
        assert view["parcel_verified"] is False


class TestIntake:
    def test_accept_lists_every_missing_document(self, api_client, application) -> None:
        """FR-04. Reporting one at a time is how the current process wastes a week."""
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        response = api_client.post(
            f"/applications/{application}/intake/accept",
            headers=hdr(CLERK),
            json={"confirmed_additional_disciplines": []},
        )
        assert response.status_code == 422
        assert len(response.json()["reasons"]) >= 5

    def test_illegal_action_is_409_not_422(self, api_client, application) -> None:
        """A 409 says retrying will not help; a 422 says fix the input and retry."""
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        response = api_client.post(f"/applications/{application}/issue", headers=hdr(SUPERVISOR))
        assert response.status_code == 409
        assert response.json()["current_state"] == "INTAKE_SCREENING"

    def test_waiver_satisfies_the_checklist(self, api_client, application) -> None:
        outstanding = api_client.post(
            f"/applications/{application}/submit", headers=hdr(APPLICANT)
        ).json()["required_documents"]

        response = api_client.post(
            f"/applications/{application}/documents/{outstanding[0]}/waive",
            headers=hdr(CLERK),
            json={"reason": "on file from the 2025 subdivision approval"},
        )
        assert response.status_code == 200
        assert outstanding[0] not in response.json()["outstanding"]

    def test_accept_opens_parallel_reviews(self, api_client, application) -> None:
        """FR-07, FR-09."""
        _satisfy_documents(api_client, application)
        response = api_client.post(
            f"/applications/{application}/intake/accept",
            headers=hdr(CLERK),
            json={"confirmed_additional_disciplines": ["ENVIRONMENTAL"]},
        )
        assert response.status_code == 200

        tasks = response.json()["review_tasks"]
        assert {t["discipline_code"] for t in tasks} == {"ZONING", "STRUCTURAL", "ENVIRONMENTAL"}
        assert all(t["reviewer"] and t["sla_due_at"] for t in tasks)


class TestReviewAndDecision:
    @pytest.fixture
    def under_review(self, api_client, application) -> str:
        _satisfy_documents(api_client, application)
        api_client.post(
            f"/applications/{application}/intake/accept",
            headers=hdr(CLERK),
            json={"confirmed_additional_disciplines": []},
        )
        return application

    def test_full_cycle_to_issuance(self, api_client, under_review) -> None:
        for discipline in ("ZONING", "STRUCTURAL"):
            task = _current_task(under_review, discipline)
            api_client.post(f"/tasks/{task['id']}/start", headers=hdr(task["username"]))
            response = api_client.post(
                f"/tasks/{task['id']}/approve",
                headers=hdr(task["username"]),
                json={"note": None},
            )
            assert response.status_code == 200

        assert response.json()["application_status"] == "PENDING_DECISION"
        issued = api_client.post(f"/applications/{under_review}/issue", headers=hdr(SUPERVISOR))
        assert issued.json()["status"] == "ISSUED"

    def test_deficiency_then_reopen(self, api_client, under_review) -> None:
        """FR-11, FR-12 over HTTP."""
        zoning = _current_task(under_review, "ZONING")
        api_client.post(f"/tasks/{zoning['id']}/start", headers=hdr(zoning["username"]))
        api_client.post(
            f"/tasks/{zoning['id']}/approve", headers=hdr(zoning["username"]), json={"note": None}
        )

        structural = _current_task(under_review, "STRUCTURAL")
        api_client.post(f"/tasks/{structural['id']}/start", headers=hdr(structural["username"]))
        response = api_client.post(
            f"/tasks/{structural['id']}/deficiencies",
            headers=hdr(structural["username"]),
            json={
                "deficiencies": [
                    {
                        "code_reference": "IRC R502.3.1",
                        "description": "Joist span exceeds the allowable table value",
                        "severity": "MAJOR",
                    }
                ]
            },
        )
        assert response.json()["application_status"] == "REVISIONS_REQUESTED"

        resubmitted = api_client.post(
            f"/applications/{under_review}/resubmit", headers=hdr(APPLICANT)
        ).json()
        round_two = [t for t in resubmitted["review_tasks"] if t["round"] == 2]
        assert len(round_two) == 1 and round_two[0]["discipline_code"] == "STRUCTURAL"

    def test_uncited_deficiency_is_rejected(self, api_client, under_review) -> None:
        task = _current_task(under_review, "STRUCTURAL")
        api_client.post(f"/tasks/{task['id']}/start", headers=hdr(task["username"]))
        response = api_client.post(
            f"/tasks/{task['id']}/deficiencies",
            headers=hdr(task["username"]),
            json={"deficiencies": [{"code_reference": "", "description": "wrong", "severity": "MAJOR"}]},
        )
        assert response.status_code == 422

    def test_reviewer_cannot_action_another_task(self, api_client, under_review) -> None:
        zoning = _current_task(under_review, "ZONING")
        structural = _current_task(under_review, "STRUCTURAL")
        response = api_client.post(
            f"/tasks/{structural['id']}/start", headers=hdr(zoning["username"])
        )
        assert response.status_code == 403

    def test_deny_requires_a_reason(self, api_client, under_review) -> None:
        for discipline in ("ZONING", "STRUCTURAL"):
            task = _current_task(under_review, discipline)
            api_client.post(f"/tasks/{task['id']}/start", headers=hdr(task["username"]))
            api_client.post(
                f"/tasks/{task['id']}/approve", headers=hdr(task["username"]), json={"note": None}
            )
        response = api_client.post(
            f"/applications/{under_review}/deny", headers=hdr(SUPERVISOR), json={"reason": "  "}
        )
        assert response.status_code == 422

    def test_history_is_retrievable(self, api_client, under_review) -> None:
        """US-16."""
        body = api_client.get(
            f"/applications/{under_review}/history", headers=hdr(SUPERVISOR)
        ).json()
        assert len(body["transitions"]) >= 3
        assert body["audit"]
        assert all(row["actor"] for row in body["audit"])


class TestAiEndpoints:
    def test_triage_returns_recommendations_only(self, api_client, application) -> None:
        """AI-04 expressed as an API shape."""
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        before = api_client.get(f"/applications/{application}", headers=hdr(CLERK)).json()["status"]

        body = api_client.post(
            f"/ai/applications/{application}/triage", headers=hdr(CLERK)
        ).json()
        assert body["field_extraction"]["accepted"]
        assert body["discipline_routing"]["recommended_additions"] == ["ENVIRONMENTAL"]

        after = api_client.get(f"/applications/{application}", headers=hdr(CLERK)).json()["status"]
        assert after == before

    def test_recommendation_decision_is_recorded(self, api_client, application) -> None:
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        body = api_client.post(
            f"/ai/applications/{application}/triage", headers=hdr(CLERK)
        ).json()
        recommendation_id = body["discipline_routing"]["recommendation_id"]

        first = api_client.post(
            f"/ai/recommendations/{recommendation_id}/decide",
            headers=hdr(CLERK),
            json={"accepted": True},
        )
        assert first.status_code == 200

        second = api_client.post(
            f"/ai/recommendations/{recommendation_id}/decide",
            headers=hdr("jtakeda"),
            json={"accepted": False},
        )
        assert second.status_code == 409

    def test_ordinance_answer_is_cited(self, api_client) -> None:
        body = api_client.get(
            "/ai/ordinance",
            headers=hdr("rokafor"),
            params={"q": "What is the minimum front setback in the R-60 district?"},
        ).json()
        assert body["answered"] is True
        assert body["citations"][0]["section"].startswith("§ 59-2.2.3")

    def test_ordinance_withholds_when_unanswerable(self, api_client) -> None:
        body = api_client.get(
            "/ai/ordinance",
            headers=hdr("rokafor"),
            params={"q": "Are drone landing pads permitted in residential districts?"},
        ).json()
        assert body["answered"] is False
        assert body["citations"] == []
        assert body["withheld_reason"]


class TestReports:
    def test_open_work_groups_by_state(self, api_client, application) -> None:
        api_client.post(f"/applications/{application}/submit", headers=hdr(APPLICANT))
        body = api_client.get("/reports/open-work", headers=hdr(SUPERVISOR)).json()
        statuses = {row["status"] for row in body["by_status"]}
        assert "INTAKE_SCREENING" in statuses

    def test_workload_is_readable(self, api_client) -> None:
        rows = api_client.get("/queues/workload", headers=hdr(SUPERVISOR)).json()
        assert rows and {"username", "discipline_code", "open_tasks"} <= set(rows[0])
