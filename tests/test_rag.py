"""Grounded ordinance answers (AI-05, AI-06).

The property under test is not answer quality. It is that a quote is always the
ordinance's own words, and that a question the corpus does not answer produces no answer
at all. An answer that sounds right and cites a provision that does not say that is how a
permit gets wrongly issued.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from permitflow.ai import rag
from permitflow.ai.provider import OfflineProvider, SupportVerdict
from permitflow.ai.retrieval import load_index, parse_sections, tokenize

ANSWERABLE = [
    "What is the minimum rear setback in the R-90 district?",
    "How far can an uncovered deck project into a required rear setback?",
    "When is a stormwater management plan required?",
    "What is the maximum height of a fence in a front setback?",
    "What is the maximum lot coverage in the R-60 district?",
    "How large may an accessory dwelling unit be?",
]

UNANSWERABLE = [
    "What is the corporate tax rate for construction firms?",
    "How many parking spaces does a helipad require?",
    "Are drone landing pads permitted in residential districts?",
    "What is the penalty for jaywalking on Copperline Boulevard?",
]


@pytest.fixture(scope="module")
def index():
    return load_index()


class TestCorpus:
    def test_sections_are_parsed(self, index) -> None:
        assert len(index) > 20
        assert index.by_id("§ 59-2.2.4") is not None

    def test_preamble_is_not_a_section(self, index) -> None:
        """Front matter before the first numbered provision must not be retrievable."""
        assert all(s.section_id.startswith("§") for s in index.sections)

    def test_section_text_excludes_the_heading(self, index) -> None:
        section = index.by_id("§ 59-2.2.4")
        assert not section.text.startswith("##")
        assert "R-90 district the minimum lot area" in section.text

    def test_parse_handles_a_document_with_no_sections(self) -> None:
        assert parse_sections("just some prose\n", source="x.md") == []


class TestAnswering:
    @pytest.mark.parametrize("question", ANSWERABLE)
    def test_answers_questions_the_corpus_covers(self, question) -> None:
        answer = rag.answer_question(question)
        assert answer.answered, f"expected an answer for: {question}"
        assert answer.citations

    @pytest.mark.parametrize("question", ANSWERABLE)
    def test_quote_is_verbatim(self, question, index) -> None:
        """AI-05. The guarantee that makes a citation worth relying on."""
        answer = rag.answer_question(question)
        for citation in answer.citations:
            section = index.by_id(citation.section_id)
            assert section is not None
            assert citation.quote in section.text

    def test_citation_carries_a_section_number(self) -> None:
        answer = rag.answer_question(ANSWERABLE[0])
        assert answer.citations[0].section_id.startswith("§")
        assert answer.citations[0].title

    def test_display_quote_only_changes_whitespace(self, index) -> None:
        answer = rag.answer_question(ANSWERABLE[0])
        citation = answer.citations[0]
        assert citation.display_quote.split() == citation.quote.split()


class TestWithholding:
    @pytest.mark.parametrize("question", UNANSWERABLE)
    def test_unanswerable_withheld(self, question) -> None:
        """AI-06. Withholding is the correct outcome, not a failure."""
        answer = rag.answer_question(question)
        assert not answer.answered
        assert not answer.citations
        assert answer.withheld_reason

    def test_withheld_answer_reports_what_was_considered(self) -> None:
        """A reviewer should be able to check the system looked in the right place."""
        answer = rag.answer_question("How many parking spaces does a helipad require?")
        assert answer.considered
        assert "distinctive terms" in answer.withheld_reason

    def test_distinctive_terms_drive_the_decision(self, index) -> None:
        """Term overlap alone is the wrong grounding metric.

        The helipad question shares "parking" and "spaces" with the residential parking
        section and looks well supported until rare terms are weighted.
        """
        parking = index.by_id("§ 59-5.1.1")
        covered = index.term_coverage("How many parking spaces are required?", parking)
        uncovered = index.term_coverage(
            "How many parking spaces does a helipad require?", parking
        )
        assert covered > uncovered

    def test_unseen_term_outweighs_common_ones(self, index) -> None:
        assert index.idf("helipad") > index.idf("setback")

    def test_a_provider_that_refuses_support_withholds(self) -> None:
        """The support judgement is a real second gate, not decoration."""

        class NeverSupports(OfflineProvider):
            def assess_support(self, question, section_id, section_text):
                return SupportVerdict(False, "declined for the test", 0.0)

        answer = rag.answer_question(ANSWERABLE[0], provider=NeverSupports())
        assert not answer.answered
        assert "declined for the test" in answer.withheld_reason


class TestVerify:
    def test_verify_passes_for_a_real_answer(self, index) -> None:
        rag.verify(rag.answer_question(ANSWERABLE[0]), index)

    def test_verify_rejects_a_tampered_quote(self, index) -> None:
        """The check that would catch someone letting a model rewrite the quote path."""
        answer = rag.answer_question(ANSWERABLE[0])
        tampered = replace(
            answer,
            citations=[replace(answer.citations[0], quote="The minimum rear setback is 12 feet.")],
        )
        with pytest.raises(AssertionError, match="verbatim"):
            rag.verify(tampered, index)

    def test_verify_rejects_an_unknown_section(self, index) -> None:
        answer = rag.answer_question(ANSWERABLE[0])
        tampered = replace(
            answer, citations=[replace(answer.citations[0], section_id="§ 99-9.9.9")]
        )
        with pytest.raises(AssertionError, match="unknown section"):
            rag.verify(tampered, index)


class TestRetrieval:
    def test_tokenize_drops_stopwords(self) -> None:
        assert "the" not in tokenize("The minimum rear setback")
        assert "setback" in tokenize("The minimum rear setback")

    def test_search_ranks_the_right_section_first(self, index) -> None:
        hits = index.search("R-90 district minimum rear setback")
        assert hits[0].section.section_id == "§ 59-2.2.4"

    def test_search_is_deterministic(self, index) -> None:
        first = [h.section.section_id for h in index.search("stormwater management plan")]
        second = [h.section.section_id for h in index.search("stormwater management plan")]
        assert first == second

    def test_empty_query_returns_nothing(self, index) -> None:
        assert index.search("the and of") == []
