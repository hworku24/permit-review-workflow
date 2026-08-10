"""Role based access at the API boundary (NFR-01).

Most authorization lives in the engine, which is the right place for it: a rule enforced
only at the HTTP layer is a rule any script driving the engine directly can skip. These
tests cover the boundary, and the engine tests cover the rule.
"""

from __future__ import annotations


def hdr(username: str) -> dict[str, str]:
    return {"X-Actor": username}


class TestActorResolution:
    def test_missing_header_is_unauthorized(self, api_client) -> None:
        assert api_client.get("/queues/intake").status_code == 401

    def test_unknown_username_is_treated_as_an_applicant(self, api_client, committed_parties) -> None:
        """Applicants are external and are not rows in the reviewer table."""
        response = api_client.post(
            "/applications",
            headers=hdr("someone-off-the-street"),
            json={
                "applicant_id": str(committed_parties["applicant_id"]),
                "parcel_id": str(committed_parties["parcel_id"]),
                "permit_type_code": "BLD-RES-ALT",
                "scope_narrative": "Rear addition, 640 sq ft.",
                "declared_valuation": "180000",
            },
        )
        assert response.status_code == 201

    def test_inactive_staff_are_refused(self, api_client) -> None:
        assert api_client.get("/queues/mine", headers=hdr("tbrandt")).status_code == 403


class TestQueueScoping:
    def test_clerk_may_read_the_intake_queue(self, api_client) -> None:
        assert api_client.get("/queues/intake", headers=hdr("mcarrero")).status_code == 200

    def test_reviewer_may_not_read_the_intake_queue(self, api_client) -> None:
        assert api_client.get("/queues/intake", headers=hdr("rokafor")).status_code == 403

    def test_only_supervisors_see_escalations(self, api_client) -> None:
        assert api_client.get("/queues/escalations", headers=hdr("dhollis")).status_code == 200
        assert api_client.get("/queues/escalations", headers=hdr("mcarrero")).status_code == 403
        assert api_client.get("/queues/escalations", headers=hdr("rokafor")).status_code == 403

    def test_only_reviewers_have_a_personal_queue(self, api_client) -> None:
        assert api_client.get("/queues/mine", headers=hdr("rokafor")).status_code == 200
        assert api_client.get("/queues/mine", headers=hdr("dhollis")).status_code == 403

    def test_only_supervisors_may_sweep(self, api_client) -> None:
        assert api_client.post("/queues/escalations/sweep", headers=hdr("mcarrero")).status_code == 403
        assert api_client.post("/queues/escalations/sweep", headers=hdr("dhollis")).status_code == 200


class TestErrorMapping:
    def test_unknown_application_is_404(self, api_client) -> None:
        response = api_client.get(
            "/applications/00000000-0000-4000-8000-000000000000", headers=hdr("dhollis")
        )
        assert response.status_code == 404

    def test_validation_error_is_422(self, api_client, committed_parties) -> None:
        response = api_client.post(
            "/applications",
            headers=hdr("priya"),
            json={
                "applicant_id": str(committed_parties["applicant_id"]),
                "parcel_id": str(committed_parties["parcel_id"]),
                "permit_type_code": "BLD-RES-ALT",
                "scope_narrative": "short",  # below the minimum length
                "declared_valuation": "180000",
            },
        )
        assert response.status_code == 422


class TestHealth:
    def test_health_reports_both_dependencies(self, api_client) -> None:
        body = api_client.get("/health").json()
        assert body["status"] == "ok"
        assert body["checks"]["database"].startswith("ok")
        assert body["checks"]["ordinance_corpus"].startswith("ok")
