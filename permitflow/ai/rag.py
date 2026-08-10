"""Grounded ordinance answers for reviewers (AI-05, AI-06).

Discovery pain point P7: reviewers answer the same zoning questions repeatedly and
sometimes inconsistently. The fix is retrieval with citation, not a model that knows
zoning. So the answer this module returns is not prose about the ordinance. It is the
ordinance's own words, sliced out of the corpus by section id, with the citation attached.

The failure this design rules out is the one that matters. An answer that sounds right and
cites a provision that does not say that is how a permit gets wrongly issued, and it is
the kind of error that only surfaces at an appeal hearing. Two properties prevent it:

1. No model ever types a quote. Quotes are read out of the corpus by section id, so every
   quote is byte-exact. `verify()` re-checks that against the corpus before returning.
2. A question the corpus does not answer is reported as unanswerable. Withholding is a
   correct outcome, and it is the only correct outcome when nothing supports an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import get_settings
from .provider import PROMPT_VERSION, Provider, get_provider
from .retrieval import Hit, OrdinanceIndex, load_index, normalized_scores, tokenize

#: Sentence-ish split. Deliberately conservative: it breaks on a period followed by
#: whitespace and a capital, so "R-90." mid-sentence and "§ 59-2.2.4" stay intact.
_SENTENCE_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z])")


@dataclass(frozen=True)
class Citation:
    """A verbatim provision and where it came from."""

    section_id: str
    title: str
    quote: str
    source: str
    relevance: float
    reason: str

    @property
    def label(self) -> str:
        return f"{self.section_id} {self.title}"

    @property
    def display_quote(self) -> str:
        """The quote with the corpus's hard line wrapping collapsed.

        `quote` stays byte-exact because that is what `verify()` checks against the
        corpus. This is the render-time version, and the only edit it makes is to
        whitespace, so no word of the ordinance changes.
        """
        return " ".join(self.quote.split())


@dataclass(frozen=True)
class OrdinanceAnswer:
    question: str
    answered: bool
    citations: list[Citation] = field(default_factory=list)
    withheld_reason: str | None = None
    considered: list[str] = field(default_factory=list)
    model: str = "offline-rules-v1"
    prompt_version: str = PROMPT_VERSION

    def render(self) -> str:
        """Plain-text answer for a reviewer.

        No synthesis, no summary sentence in the system's own voice. The reviewer reads
        the ordinance and decides, which is the only version of this feature the
        department was willing to accept.
        """
        if not self.answered:
            return (
                f"No provision in the adopted ordinance answers this question.\n"
                f"Reason: {self.withheld_reason}\n"
                f"Sections considered: {', '.join(self.considered) or 'none'}"
            )
        lines = [f"Question: {self.question}", ""]
        for c in self.citations:
            lines.append(f"{c.label}")
            lines.append(f"  \"{c.display_quote}\"")
            lines.append(f"  ({c.source}, relevance {c.relevance:.2f})")
            lines.append("")
        return "\n".join(lines).rstrip()


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Character offsets of each sentence, so quotes can be sliced rather than rebuilt."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_RE.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _best_quote(section_text: str, question: str) -> str:
    """Pick the most relevant verbatim span of a section.

    Always a contiguous slice of `section_text`, never a rewrite and never a stitch of
    non-adjacent sentences. Stitching was the original implementation and it produced text
    that appeared nowhere in the corpus, which `verify()` caught immediately. Taking the
    span from the first to the last relevant sentence keeps the quote byte-exact and has
    the side benefit of carrying the qualifying clauses in between.
    """
    q_terms = set(tokenize(question))
    spans = _sentence_spans(section_text)
    if not q_terms or len(spans) <= 1:
        return section_text.strip()

    scored = [(len(q_terms & set(tokenize(section_text[a:b]))), a, b) for a, b in spans]
    best_overlap = max(s[0] for s in scored)
    if best_overlap == 0:
        return section_text.strip()

    keep = [s for s in scored if s[0] == best_overlap]
    start = min(s[1] for s in keep)
    end = max(s[2] for s in keep)
    return section_text[start:end].strip()


def answer_question(
    question: str,
    *,
    index: OrdinanceIndex | None = None,
    provider: Provider | None = None,
    limit: int = 5,
) -> OrdinanceAnswer:
    """Answer a reviewer's ordinance question, or decline to (AI-05, AI-06)."""
    settings = get_settings()
    index = index or load_index()
    provider = provider or get_provider()

    hits: list[Hit] = index.search(question, limit=limit)
    if not hits:
        return OrdinanceAnswer(
            question=question,
            answered=False,
            withheld_reason="no ordinance section matched the question",
            model=provider.model,
        )

    considered = [h.section.section_id for h in hits]
    citations: list[Citation] = []
    rejections: list[str] = []

    for hit, relative in normalized_scores(hits):
        # Gate one: does this section contain the question's distinctive terms at all?
        # This is the check that withholds on a question about something the ordinance
        # does not cover, even when it shares common vocabulary with a real section.
        coverage = index.term_coverage(question, hit.section)
        if coverage < settings.ai_grounding_threshold:
            rejections.append(
                f"{hit.section.section_id} covers only {coverage:.0%} of the question's "
                f"distinctive terms"
            )
            continue

        # Gate two: does it actually address what was asked, rather than sharing a topic?
        verdict = provider.assess_support(question, hit.section.section_id, hit.section.text)
        if not verdict.supports:
            rejections.append(f"{hit.section.section_id}: {verdict.reason}")
            continue

        # Read the quote back out of the corpus by id. Nothing above this line produced
        # any provision text, and nothing below it edits what came out.
        section = index.by_id(hit.section.section_id)
        if section is None:  # pragma: no cover - id came from the index itself
            rejections.append(f"{hit.section.section_id} is not in the corpus")
            continue

        citations.append(
            Citation(
                section_id=section.section_id,
                title=section.title,
                quote=_best_quote(section.text, question),
                source=section.source,
                relevance=round(relative, 3),
                reason=verdict.reason,
            )
        )

    if not citations:
        return OrdinanceAnswer(
            question=question,
            answered=False,
            withheld_reason="; ".join(rejections) or "no section supported an answer",
            considered=considered,
            model=provider.model,
        )

    answer = OrdinanceAnswer(
        question=question,
        answered=True,
        citations=citations,
        considered=considered,
        model=provider.model,
    )
    verify(answer, index)
    return answer


def verify(answer: OrdinanceAnswer, index: OrdinanceIndex | None = None) -> None:
    """Assert every quote appears byte for byte in the corpus.

    This should be impossible to fail given how quotes are produced, which is exactly why
    it is worth asserting. It is the check that would catch someone later "improving" the
    quote path by letting a model rewrite the text.
    """
    index = index or load_index()
    for citation in answer.citations:
        section = index.by_id(citation.section_id)
        if section is None:
            raise AssertionError(f"citation references unknown section {citation.section_id}")
        if citation.quote not in section.text:
            raise AssertionError(
                f"quote for {citation.section_id} is not present verbatim in the corpus"
            )
