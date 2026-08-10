"""Ordinance corpus loading and lexical retrieval.

BM25 over the zoning ordinance, implemented here rather than pulled in as a dependency.
Two reasons: NFR-06 wants a fresh clone to work with no external services, and a legal
corpus is exactly the case where lexical matching is a defensible default. A reviewer
asking about "rear setback in R-90" wants the section that literally says those words, not
the semantically nearest neighbour.

Section text is stored verbatim. Nothing in this module or in `rag.py` ever paraphrases
it, because the citation a reviewer relies on has to be the ordinance's words.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parents[2] / "corpus" / "zoning-ordinance"

#: Standard BM25 parameters. k1 controls term-frequency saturation, b the length
#: normalization. Left at the usual values; the corpus is too small for tuning them to
#: mean anything.
K1 = 1.5
B = 0.75

_SECTION_RE = re.compile(r"^##\s+(§\s*[\d\-.]+)\s+(.*)$", re.M)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")

STOPWORDS = frozenset("""
a an and are as at be by for from has have in is it its of on or shall the this to with
""".split())


@dataclass(frozen=True)
class Section:
    """One numbered provision, exactly as published."""

    section_id: str
    title: str
    text: str
    source: str

    @property
    def citation(self) -> str:
        return f"{self.section_id} {self.title}"


@dataclass(frozen=True)
class Hit:
    section: Section
    score: float


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def parse_sections(text: str, source: str) -> list[Section]:
    """Split a corpus file into numbered sections.

    Content before the first `## §` heading is preamble and is dropped, which is how the
    document's own front matter stays out of retrieval results.
    """
    matches = list(_SECTION_RE.finditer(text))
    sections: list[Section] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append(
            Section(
                section_id=match.group(1).replace("§ ", "§ ").strip(),
                title=match.group(2).strip(),
                text=body,
                source=source,
            )
        )
    return sections


class OrdinanceIndex:
    """BM25 index over the ordinance sections."""

    def __init__(self, sections: list[Section]) -> None:
        self.sections = sections
        self._docs = [tokenize(f"{s.title} {s.text}") for s in sections]
        self._lengths = [len(d) for d in self._docs]
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._tf = [Counter(d) for d in self._docs]

        df: Counter[str] = Counter()
        for doc in self._docs:
            df.update(set(doc))
        n = len(self._docs)
        # BM25 idf with the +0.5 smoothing, floored at a small positive value so a term
        # appearing in most sections contributes almost nothing rather than a negative
        # score that would rank an irrelevant section above a relevant one.
        self._idf = {
            term: max(1e-6, math.log(1 + (n - count + 0.5) / (count + 0.5)))
            for term, count in df.items()
        }
        #: What a term that appears in no section is worth. Same formula at count = 0.
        #: A question containing one of these is asking about something the ordinance does
        #: not cover, and `term_coverage` uses that fact to withhold rather than answer.
        self._unseen_idf = math.log(1 + (n + 0.5) / 0.5)

    def __len__(self) -> int:
        return len(self.sections)

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        terms = tokenize(query)
        if not terms:
            return []

        scored: list[Hit] = []
        for i, section in enumerate(self.sections):
            score = 0.0
            length = self._lengths[i] or 1
            for term in terms:
                freq = self._tf[i].get(term, 0)
                if not freq:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = freq + K1 * (1 - B + B * length / (self._avg_length or 1))
                score += idf * (freq * (K1 + 1)) / denom
            if score > 0:
                scored.append(Hit(section=section, score=score))

        scored.sort(key=lambda h: (-h.score, h.section.section_id))
        return scored[:limit]

    def idf(self, term: str) -> float:
        return self._idf.get(term, self._unseen_idf)

    def term_coverage(self, query: str, section: Section) -> float:
        """Fraction of the query's information content that this section actually contains.

        Plain term overlap is the wrong grounding metric, and the failure is easy to
        reproduce: "how many parking spaces does a helipad require" overlaps the
        residential parking section on "parking" and "spaces" and looks well supported,
        even though the ordinance says nothing about helipads. Weighting by inverse
        document frequency fixes it. Common words such as "parking" are worth almost
        nothing; "helipad" appears in no section, carries the unseen-term weight, and
        drags coverage down until the answer is withheld.

        Returns 0.0 to 1.0.
        """
        terms = set(tokenize(query))
        if not terms:
            return 0.0
        present = set(tokenize(f"{section.title} {section.text}"))
        total = sum(self.idf(t) for t in terms)
        if total <= 0:
            return 0.0
        matched = sum(self.idf(t) for t in terms if t in present)
        return matched / total

    def by_id(self, section_id: str) -> Section | None:
        """Fetch a section by its number.

        This is the function that makes citations trustworthy. The answer path retrieves a
        section id, then reads the text back out of the corpus by that id. No model ever
        types a quote, so every quote is byte-exact by construction.
        """
        normalized = section_id.strip()
        for section in self.sections:
            if section.section_id == normalized:
                return section
        return None


@lru_cache(maxsize=1)
def load_index(corpus_dir: Path | None = None) -> OrdinanceIndex:
    directory = corpus_dir or CORPUS_DIR
    sections: list[Section] = []
    for path in sorted(directory.glob("*.md")):
        sections.extend(parse_sections(path.read_text(), source=path.name))
    if not sections:
        raise RuntimeError(f"no ordinance sections found under {directory}")
    return OrdinanceIndex(sections)


def normalized_scores(hits: list[Hit]) -> list[tuple[Hit, float]]:
    """Scale BM25 scores into 0..1 against the best hit in this result set.

    Raw BM25 has no absolute meaning, so the grounding threshold in `rag.py` is applied to
    a relative score. The consequence worth knowing: the top hit always scores 1.0, which
    is why the threshold alone is not the grounding check. The support judgement in
    `provider.assess_support` is the gate that actually rejects an off-topic best match.
    """
    if not hits:
        return []
    top = hits[0].score or 1.0
    return [(h, h.score / top) for h in hits]
