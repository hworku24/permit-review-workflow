"""The reviewer queue screen.

Asserts on what a reviewer would see, not on markup: their own case numbers present,
somebody else's absent, the overdue flag showing when a task is past its allowance. A test
that pins class names breaks on a restyle and tells nobody anything.
"""

from __future__ import annotations

from permitflow.ui.deps import ACTOR_COOKIE


def signed_in(client, username: str):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


class TestSignIn:
    def test_queue_without_a_cookie_goes_to_the_picker(self, api_client) -> None:
        response = api_client.get("/ui/queue", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/ui/?next=")

    def test_the_picker_lists_staff(self, api_client) -> None:
        body = api_client.get("/ui/").text
        assert "pvasquez" in body
        assert "Who are you today?" in body

    def test_signing_in_sets_the_cookie_and_lands_on_the_queue(self, api_client) -> None:
        response = api_client.post(
            "/ui/sign-in",
            data={"username": "pvasquez", "next": "/ui/queue"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/queue"
        assert response.cookies[ACTOR_COOKIE] == "pvasquez"

    def test_a_deactivated_account_cannot_reach_the_queue(self, api_client) -> None:
        """tbrandt is seeded inactive so one account proves the check runs."""
        signed_in(api_client, "tbrandt")
        response = api_client.get("/ui/queue", follow_redirects=False)
        assert response.status_code == 303

    def test_an_unknown_username_is_sent_back_to_the_picker(self, api_client) -> None:
        signed_in(api_client, "nobody-here")
        assert api_client.get("/ui/queue", follow_redirects=False).status_code == 303


class TestQueueContents:
    def test_a_reviewer_sees_their_own_open_task(self, api_client, application_under_review) -> None:
        case = application_under_review
        signed_in(api_client, case["reviewer_username"])
        body = api_client.get("/ui/queue").text
        assert case["application_number"] in body
        assert case["discipline_code"] in body

    def test_a_reviewer_does_not_see_another_reviewer_s_work(
        self, api_client, application_under_review
    ) -> None:
        case = application_under_review
        other = case["other_reviewer_username"]
        signed_in(api_client, other)
        body = api_client.get("/ui/queue").text
        assert case["application_number"] not in body

    def test_every_row_links_to_the_case(self, api_client, application_under_review) -> None:
        case = application_under_review
        signed_in(api_client, case["reviewer_username"])
        body = api_client.get("/ui/queue").text
        assert f'/ui/case/{case["application_id"]}' in body

    def test_an_overdue_task_is_flagged(self, api_client, overdue_task) -> None:
        signed_in(api_client, overdue_task["reviewer_username"])
        body = api_client.get("/ui/queue").text
        assert "Overdue" in body
        assert "overdue" in body.lower()

    def test_a_clerk_is_told_the_screen_is_not_theirs(self, api_client) -> None:
        """An empty table would read as a broken query. Say which role holds tasks."""
        signed_in(api_client, "mcarrero")
        body = api_client.get("/ui/queue").text
        assert "only reviewers hold those" in body.lower()

    def test_a_reviewer_with_nothing_assigned_sees_a_plain_message(self, api_client) -> None:
        signed_in(api_client, "clindgren")
        body = api_client.get("/ui/queue").text
        assert "Nothing assigned to you" in body


class TestStatic:
    def test_the_stylesheet_is_served(self, api_client) -> None:
        response = api_client.get("/ui/static/app.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
