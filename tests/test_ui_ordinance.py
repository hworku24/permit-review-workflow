"""The ordinance question screen.

The claim this screen makes is that every quote on it appears verbatim in the corpus. The
tests check that claim against the corpus itself, not against the page's own say-so, and
check that a verification failure withholds the answer, and does not show it with a
warning attached.
"""

from __future__ import annotations

import pytest

from permitflow.ai.retrieval import load_index
from permitflow.ui.deps import ACTOR_COOKIE

SETBACK = "What is the rear setback in R-90?"
HELIPAD = "How many parking spaces does a helipad require?"


def signed_in(client, username: str = "pvasquez"):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


class TestAccess:
    def test_without_a_cookie_it_redirects(self, api_client) -> None:
        assert api_client.get("/ui/ordinance", follow_redirects=False).status_code == 303

    def test_the_empty_screen_offers_examples(self, api_client) -> None:
        signed_in(api_client)
        body = api_client.get("/ui/ordinance").text
        assert "Try one of these" in body
        assert "helipad" in body


class TestAnswering:
    def test_it_returns_a_section_number_and_a_title(self, api_client) -> None:
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": SETBACK}).text
        assert "59-2.2.4" in body
        assert "dimensional standards" in body

    def test_the_quote_on_the_page_appears_verbatim_in_the_corpus(self, api_client) -> None:
        """The whole point. Checked against the corpus, not against the page's own claim."""
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": SETBACK}).text

        index = load_index()
        section = index.by_id("§ 59-2.2.4")
        assert section is not None
        # Every sentence of the quoted provision that reached the page must be in the source.
        assert "The minimum rear setback is 30 feet." in body
        assert "The minimum rear setback is 30 feet." in " ".join(section.text.split())

    def test_it_offers_the_whole_provision_the_quote_came_from(self, api_client) -> None:
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": SETBACK}).text
        assert "Read the whole provision" in body

    def test_it_shows_which_sections_were_considered(self, api_client) -> None:
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": SETBACK}).text
        assert "Sections considered" in body


class TestWithholding:
    def test_a_question_the_ordinance_does_not_answer_comes_back_unanswered(
        self, api_client
    ) -> None:
        """Overlaps residential parking on 'parking' and 'spaces' and is still unanswerable."""
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": HELIPAD}).text
        assert "No answer in the ordinance" in body
        assert "Sections considered" in body

    def test_an_unanswered_question_quotes_nothing(self, api_client) -> None:
        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": HELIPAD}).text
        assert "provision" not in body.split("No answer in the ordinance")[0].split("<h2>")[-1]
        assert "Read the whole provision" not in body


class TestVerificationFailure:
    def test_a_failed_verification_withholds_the_whole_answer(
        self, api_client, monkeypatch
    ) -> None:
        """A quote that cannot be found in the ordinance is not a quote.

        Standing in for a later change that let a model rewrite the quote text. The page
        has to lose the answer, not show it with a caveat.
        """
        from permitflow.ui import routes

        def boom(answer, index=None):
            raise AssertionError("quote for § 59-2.2.4 is not present verbatim in the corpus")

        # Patched on the rag module, which is where answer_question calls it from.
        monkeypatch.setattr(routes.rag, "verify", boom)

        signed_in(api_client)
        body = api_client.get("/ui/ordinance", params={"q": SETBACK}).text

        assert "Answer withheld" in body
        assert "not present verbatim" in body
        assert "The minimum rear setback is 30 feet." not in body

    def test_verification_runs_on_every_answer(self, api_client, monkeypatch) -> None:
        """Once per answer, inside answer_question. Not something the screen can skip."""
        from permitflow.ai import rag

        calls: list[str] = []
        original = rag.verify

        def counting(answer, index=None):
            calls.append(answer.question)
            return original(answer, index)

        monkeypatch.setattr(rag, "verify", counting)

        signed_in(api_client)
        api_client.get("/ui/ordinance", params={"q": SETBACK})
        assert calls == [SETBACK]


class TestCaseContext:
    def test_asking_from_a_case_links_back_to_it(self, api_client, application_under_review) -> None:
        signed_in(api_client)
        case_id = application_under_review["application_id"]
        body = api_client.get("/ui/ordinance", params={"q": SETBACK, "case": str(case_id)}).text
        assert f'href="/ui/case/{case_id}"' in body

    def test_the_case_page_links_to_the_ordinance(self, api_client, application_under_review) -> None:
        signed_in(api_client)
        case_id = application_under_review["application_id"]
        body = api_client.get(f"/ui/case/{case_id}").text
        assert f"/ui/ordinance?case={case_id}" in body


@pytest.mark.parametrize(
    "question",
    [
        "When is a stormwater management plan required?",
        "What is the maximum building height in C-2?",
    ],
)
def test_every_offered_example_either_answers_or_declines_cleanly(api_client, question) -> None:
    """No example on the empty screen may 500. A demo question that errors is worse than none."""
    signed_in(api_client)
    response = api_client.get("/ui/ordinance", params={"q": question})
    assert response.status_code == 200
    assert "Answer withheld" not in response.text
