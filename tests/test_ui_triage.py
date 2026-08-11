"""The triage screen.

The screen exists to show two things next to each other: what the model drafted, and what
it declined to draft. The second is the one worth testing, since a below-threshold field
appearing as a filled-in value is the failure the confidence rule exists to prevent.
"""

from __future__ import annotations

from permitflow.ui.deps import ACTOR_COOKIE


def signed_in(client, username: str):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


class TestAccess:
    def test_without_a_cookie_it_redirects(self, api_client, triaged_case) -> None:
        response = api_client.get(
            f"/ui/case/{triaged_case['application_id']}/triage", follow_redirects=False
        )
        assert response.status_code == 303

    def test_an_unknown_case_is_a_404(self, api_client) -> None:
        signed_in(api_client, "mcarrero")
        missing = "00000000-0000-4000-8000-000000000000"
        assert api_client.get(f"/ui/case/{missing}/triage").status_code == 404


class TestWhatItShows:
    def test_it_shows_the_applicant_s_own_words(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "mixed-use building" in body

    def test_it_shows_a_drafted_field_with_its_evidence(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "square_feet" in body
        assert "3,100 sq ft" in body

    def test_a_withheld_field_shows_blank_and_never_its_value(
        self, api_client, triaged_case
    ) -> None:
        """AI-02. The evidence span is shown; the guess itself is not."""
        signed_in(api_client, "mcarrero")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "Held back" in body
        assert "occupancy_class" in body
        assert "blank, for the clerk to key" in body
        # The low-confidence guess was B. It must not appear as a value in the table.
        assert "<strong>B</strong>" not in body

    def test_it_shows_the_confidence_and_the_threshold(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "0.70" in body
        assert "0.52" in body

    def test_it_shows_the_routing_recommendation(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "Discipline routing" in body
        assert "Always required for this permit type" in body


class TestDeciding:
    def test_a_clerk_can_accept(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        case_id = triaged_case["application_id"]
        response = api_client.post(
            f"/ui/case/{case_id}/triage",
            data={"recommendation_id": str(triaged_case["recommendation_id"]), "action": "accept"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        body = api_client.get(f"/ui/case/{case_id}/triage").text
        assert "accepted" in body
        assert "mcarrero" in body

    def test_an_override_records_the_reason(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        case_id = triaged_case["application_id"]
        api_client.post(
            f"/ui/case/{case_id}/triage",
            data={
                "recommendation_id": str(triaged_case["recommendation_id"]),
                "action": "override",
                "reason": "cost affidavit says 265,000",
            },
            follow_redirects=False,
        )
        body = api_client.get(f"/ui/case/{case_id}/triage").text
        assert "overridden" in body
        assert "cost affidavit says 265,000" in body

    def test_an_override_without_a_reason_is_refused(self, api_client, triaged_case) -> None:
        """An override with no reason is an audit row that explains nothing later."""
        signed_in(api_client, "mcarrero")
        response = api_client.post(
            f"/ui/case/{triaged_case['application_id']}/triage",
            data={
                "recommendation_id": str(triaged_case["recommendation_id"]),
                "action": "override",
                "reason": "   ",
            },
        )
        assert response.status_code == 422

    def test_a_reviewer_may_not_decide(self, api_client, triaged_case) -> None:
        signed_in(api_client, "pvasquez")
        response = api_client.post(
            f"/ui/case/{triaged_case['application_id']}/triage",
            data={"recommendation_id": str(triaged_case["recommendation_id"]), "action": "accept"},
        )
        assert response.status_code == 403

    def test_a_reviewer_sees_the_screen_as_read_only(self, api_client, triaged_case) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{triaged_case['application_id']}/triage").text
        assert "read only for you" in body

    def test_the_decision_is_written_to_the_audit_log(self, api_client, triaged_case) -> None:
        from permitflow.db import read_connection

        signed_in(api_client, "mcarrero")
        api_client.post(
            f"/ui/case/{triaged_case['application_id']}/triage",
            data={"recommendation_id": str(triaged_case["recommendation_id"]), "action": "accept"},
            follow_redirects=False,
        )
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT action, actor FROM audit_log WHERE entity_id = %s AND entity_type = 'ai'"
                " AND action IN ('accept','override')",
                (str(triaged_case["recommendation_id"]),),
            )
            row = cur.fetchone()
        assert row["action"] == "accept"
        assert row["actor"] == "mcarrero"

    def test_a_second_decision_on_the_same_row_is_refused(self, api_client, triaged_case) -> None:
        signed_in(api_client, "mcarrero")
        case_id = triaged_case["application_id"]
        payload = {
            "recommendation_id": str(triaged_case["recommendation_id"]),
            "action": "accept",
        }
        assert api_client.post(f"/ui/case/{case_id}/triage", data=payload,
                               follow_redirects=False).status_code == 303
        # The first decision is the one that moved the case, so the second is an error.
        import pytest

        with pytest.raises(ValueError):
            api_client.post(f"/ui/case/{case_id}/triage", data=payload)
