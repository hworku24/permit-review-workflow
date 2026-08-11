"""The case detail screen.

The screen has to hold the parts of a case that live in different tables and still read as
one record: parties, documents, every review round, the clock, and the audit trail. These
tests check that each of those actually reaches the page, and that round 1 survives a
resubmission opening round 2.
"""

from __future__ import annotations

from permitflow.ui.deps import ACTOR_COOKIE


def signed_in(client, username: str):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


class TestAccess:
    def test_without_a_cookie_it_redirects_to_the_picker(
        self, api_client, application_under_review
    ) -> None:
        response = api_client.get(
            f"/ui/case/{application_under_review['application_id']}", follow_redirects=False
        )
        assert response.status_code == 303

    def test_an_unknown_case_is_a_404(self, api_client) -> None:
        signed_in(api_client, "pvasquez")
        missing = "00000000-0000-4000-8000-000000000000"
        assert api_client.get(f"/ui/case/{missing}").status_code == 404

    def test_a_clerk_may_read_a_case(self, api_client, application_under_review) -> None:
        """Clerks have no review tasks but still answer the phone about a case."""
        signed_in(api_client, "mcarrero")
        response = api_client.get(f"/ui/case/{application_under_review['application_id']}")
        assert response.status_code == 200
        assert application_under_review["application_number"] in response.text


class TestContents:
    def test_it_shows_the_parties_and_the_parcel(
        self, api_client, application_under_review
    ) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert "API Applicant" in body
        assert "Harlowe Building Group LLC" in body
        assert "1408 Aldergate Ln" in body
        assert "14-220-118" in body

    def test_it_shows_the_document_checklist(self, api_client, application_under_review) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert "Documents" in body
        assert "RECEIVED" in body

    def test_it_shows_the_review_tasks_and_who_holds_them(
        self, api_client, application_under_review
    ) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert application_under_review["discipline_code"] in body
        assert "Reviews" in body

    def test_it_shows_the_clock(self, api_client, application_under_review) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert "Council standard" in body
        assert "Net elapsed" in body

    def test_it_shows_the_timeline_and_the_audit_trail(
        self, api_client, application_under_review
    ) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert "Timeline" in body
        assert "Audit trail" in body
        # Written by the engine on the way to UNDER_REVIEW.
        assert "transition:accept_intake" in body

    def test_the_audit_trail_includes_task_events_and_not_only_the_application(
        self, api_client, application_under_review
    ) -> None:
        """Reading only entity_type='application' would hide who was assigned what."""
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert "review_task" in body

    def test_it_links_back_to_the_queue(self, api_client, application_under_review) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{application_under_review['application_id']}").text
        assert 'href="/ui/queue"' in body


class TestPausedClock:
    def test_a_case_with_the_applicant_says_the_clock_is_stopped(
        self, api_client, returned_incomplete
    ) -> None:
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{returned_incomplete['application_id']}").text
        assert "The clock is stopped" in body
        assert "SLA clock paused" in body


class TestSecondRound:
    def test_round_one_is_still_on_the_page_after_round_two_opens(
        self, api_client, resubmitted_after_deficiency
    ) -> None:
        case = resubmitted_after_deficiency
        signed_in(api_client, "pvasquez")
        body = api_client.get(f"/ui/case/{case['application_id']}").text
        assert case["deficiency_code"] in body
        # Both rounds present in the reviews table.
        assert body.count("<td class=\"num\">1</td>") >= 1
        assert "DEFICIENT" in body
