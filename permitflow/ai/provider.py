"""Model provider abstraction.

Two implementations behind one interface. `OfflineProvider` is deterministic and needs no
credentials, which is what makes NFR-06 hold and what lets CI run the AI tests. The hosted
provider uses Claude when a key is configured.

The interface is deliberately narrow. Neither provider is ever asked to produce ordinance
text, a decision, or a status change. They do two things: pull structured fields out of a
free-text narrative, and judge whether a retrieved passage supports a question. Everything
else is code.

That split is the whole design. A model that searches, quotes, and then grades its own
quote agrees with itself. Retrieval and provenance are things a database does correctly,
so they stay in `retrieval.py`. Judgement is the only part worth a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from ..config import get_settings

PROMPT_VERSION = "2026-08-03.1"


@dataclass(frozen=True)
class FieldSuggestion:
    """One extracted field with the evidence behind it.

    `evidence` is the span of the narrative the value came from. It exists so the intake
    clerk can check the suggestion against the source in one glance rather than rereading
    the whole narrative, and so a wrong extraction is diagnosable a year later.
    """

    name: str
    value: Any
    confidence: float
    evidence: str | None = None


@dataclass(frozen=True)
class SupportVerdict:
    """Whether a retrieved ordinance section answers the question asked."""

    supports: bool
    reason: str
    confidence: float


class Provider(Protocol):
    name: str
    model: str

    def extract_fields(self, narrative: str) -> list[FieldSuggestion]: ...

    def assess_support(self, question: str, section_id: str, section_text: str) -> SupportVerdict: ...


# ---------------------------------------------------------------------------
# Offline provider
# ---------------------------------------------------------------------------

WORK_TYPE_PATTERNS: list[tuple[str, str, float]] = [
    (r"\bnew (?:single[- ]family |two[- ]family )?(?:home|house|dwelling|construction)\b", "NEW_CONSTRUCTION", 0.92),
    (r"\bground[- ]up\b", "NEW_CONSTRUCTION", 0.88),
    (r"\b(?:rear|side|second[- ]stor(?:y|ey)|two[- ]stor(?:y|ey))? ?addition\b", "ADDITION", 0.90),
    (r"\btenant (?:fit[- ]?out|improvement|build[- ]?out)\b", "TENANT_FITOUT", 0.93),
    (r"\b(?:interior )?(?:renovation|remodel|alteration)\b", "ALTERATION", 0.85),
    (r"\b(?:deck|porch|patio cover)\b", "ACCESSORY_DECK", 0.88),
    (r"\b(?:detached garage|shed|accessory structure|carport)\b", "ACCESSORY_STRUCTURE", 0.88),
    (r"\b(?:demolition|demolish|tear[- ]down)\b", "DEMOLITION", 0.90),
    # Below the threshold on purpose. "Improvements" is what an applicant writes when
    # they have not said what the work is, and an extractor that scores it like a
    # tenant fit-out is claiming a confidence it does not have. Matched last, so any
    # specific phrase above wins.
    (r"\b(?:improvements?|upgrades?|refresh|modernisation|modernization)\b", "ALTERATION", 0.58),
]

OCCUPANCY_PATTERNS: list[tuple[str, str, float]] = [
    (r"\b(R-[1-4]|B|M|A-[1-5]|S-[12]|F-[12]|E|I-[1-4])\b(?!\d)", None, 0.95),  # explicit code
    (r"\bsingle[- ]family\b", "R-3", 0.82),
    (r"\b(?:townhouse|duplex|two[- ]family)\b", "R-3", 0.78),
    (r"\b(?:apartment|multifamily|multi[- ]family)\b", "R-2", 0.80),
    (r"\boffice\b", "B", 0.80),
    (r"\b(?:retail|storefront|shop)\b", "M", 0.80),
    (r"\b(?:restaurant|cafe|bar|taproom)\b", "A-2", 0.82),
    (r"\b(?:warehouse|storage)\b", "S-1", 0.78),
    # Mixed use narrows the occupancy to several possibilities and not to one. Scored
    # below the threshold so the clerk is shown a blank and the evidence, and picks.
    (r"\bmixed[- ]use\b", "B", 0.52),
]

DISCIPLINE_SIGNALS: dict[str, list[tuple[str, float]]] = {
    "ENVIRONMENTAL": [
        (r"\bimpervious\b", 0.88), (r"\bstormwater\b", 0.92), (r"\bgrading\b", 0.80),
        (r"\bland disturb", 0.85), (r"\btree\b", 0.72), (r"\bfloodplain\b", 0.93),
        (r"\bdrainage\b", 0.75), (r"\berosion\b", 0.85),
    ],
    "FIRE": [
        (r"\bsprinkler\b", 0.92), (r"\bsuppression\b", 0.90), (r"\begress\b", 0.88),
        (r"\bfire alarm\b", 0.90), (r"\boccupant load\b", 0.85), (r"\bassembly\b", 0.72),
        (r"\bcommercial kitchen\b", 0.86), (r"\bstandpipe\b", 0.90),
    ],
    "STRUCTURAL": [
        (r"\bload[- ]bearing\b", 0.92), (r"\bfoundation\b", 0.85), (r"\bframing\b", 0.80),
        (r"\bbeam\b", 0.82), (r"\bjoist\b", 0.82), (r"\bfooting\b", 0.85),
        (r"\bretaining wall\b", 0.88), (r"\bstructural\b", 0.80),
    ],
    "ZONING": [
        (r"\bsetback\b", 0.90), (r"\blot coverage\b", 0.90), (r"\bheight\b", 0.72),
        (r"\bvariance\b", 0.88), (r"\baccessory dwelling\b", 0.90), (r"\bparking\b", 0.75),
    ],
}


@dataclass
class OfflineProvider:
    """Deterministic rule-based extraction. No network, no key, no variance.

    This is the default and the CI path. It is genuinely useful rather than a stub: the
    fields it extracts are the ones the clerk retypes most often, and the rules are
    inspectable, which matters more to a permitting department than marginal accuracy.

    Its confidence scores are calibrated to the specificity of the pattern that matched.
    "stormwater" is a much stronger signal for environmental review than "tree", and the
    scores say so, which is what makes the AI-02 threshold do real work.
    """

    name: str = "offline"
    model: str = field(default_factory=lambda: get_settings().ai_model)

    def extract_fields(self, narrative: str) -> list[FieldSuggestion]:
        text = narrative.lower()
        out: list[FieldSuggestion] = []

        for pattern, value, confidence in WORK_TYPE_PATTERNS:
            match = re.search(pattern, text)
            if match:
                out.append(FieldSuggestion("work_type", value, confidence, match.group(0)))
                break

        valuation = self._extract_valuation(narrative)
        if valuation is not None:
            out.append(valuation)

        square_feet = self._extract_square_feet(narrative)
        if square_feet is not None:
            out.append(square_feet)

        occupancy = self._extract_occupancy(narrative, text)
        if occupancy is not None:
            out.append(occupancy)

        for discipline, signals in DISCIPLINE_SIGNALS.items():
            best: tuple[float, str] | None = None
            for pattern, confidence in signals:
                match = re.search(pattern, text)
                if match and (best is None or confidence > best[0]):
                    best = (confidence, match.group(0))
            if best is not None:
                out.append(
                    FieldSuggestion(f"discipline:{discipline}", True, best[0], best[1])
                )

        return out

    def _extract_valuation(self, narrative: str) -> FieldSuggestion | None:
        """Three readings of a figure, scored by how much the wording commits to it.

        A dollar sign is an applicant stating a value. "Valued at" without one is nearly
        as good. A bare number sitting near the word budget is a guess, and it is scored
        below the threshold so the clerk gets a blank and the evidence span instead of a
        number that looks keyed in.
        """
        # "$180,000" or "$1.2 million"
        match = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|m\b|k\b)?", narrative, re.I)
        if match:
            amount = Decimal(match.group(1).replace(",", ""))
            suffix = (match.group(2) or "").lower()
            if suffix.startswith("m"):
                amount *= 1_000_000
            elif suffix.startswith("k"):
                amount *= 1_000
            return FieldSuggestion("declared_valuation", amount, 0.90, match.group(0))

        # "valued at 180000"
        match = re.search(r"valu\w*\s+(?:at\s+)?\$?\s?([\d,]+)", narrative, re.I)
        if match:
            amount = Decimal(match.group(1).replace(",", ""))
            return FieldSuggestion("declared_valuation", amount, 0.74, match.group(0))

        # A bare figure near a cost word, with no currency marker at all. As likely to be
        # a square footage or a unit count as a valuation.
        match = re.search(
            r"(?:cost|budget|estimate\w*)\D{0,20}?([\d][\d,]{3,})", narrative, re.I
        )
        if match:
            amount = Decimal(match.group(1).replace(",", ""))
            return FieldSuggestion("declared_valuation", amount, 0.61, match.group(0))

        return None

    def _extract_square_feet(self, narrative: str) -> FieldSuggestion | None:
        match = re.search(
            r"([\d,]+)\s*(?:sq\.?\s*ft\.?|square\s+feet|sf\b)", narrative, re.I
        )
        if not match:
            return None
        return FieldSuggestion(
            "square_feet", int(match.group(1).replace(",", "")), 0.91, match.group(0)
        )

    def _extract_occupancy(self, narrative: str, lowered: str) -> FieldSuggestion | None:
        explicit = re.search(
            r"\boccupancy\s+(?:group|class(?:ification)?)?\s*[:\-]?\s*"
            r"(R-[1-4]|A-[1-5]|S-[12]|F-[12]|I-[1-4]|[BME])\b",
            narrative,
            re.I,
        )
        if explicit:
            return FieldSuggestion("occupancy_class", explicit.group(1).upper(), 0.95, explicit.group(0))

        for pattern, value, confidence in OCCUPANCY_PATTERNS[1:]:
            match = re.search(pattern, lowered)
            if match:
                return FieldSuggestion("occupancy_class", value, confidence, match.group(0))
        return None

    def assess_support(self, question: str, section_id: str, section_text: str) -> SupportVerdict:
        """Term-overlap judgement.

        Weaker than a model, and honest about it. The retrieval threshold in `rag.py` does
        most of the work; this is the second gate. Because it never invents text, a wrong
        verdict here withholds or surfaces a real ordinance section, and it can never
        produce a fabricated one.
        """
        q_terms = _content_terms(question)
        s_terms = _content_terms(section_text)
        if not q_terms:
            return SupportVerdict(False, "no substantive terms in the question", 0.0)

        overlap = q_terms & s_terms
        ratio = len(overlap) / len(q_terms)
        supports = ratio >= 0.34
        return SupportVerdict(
            supports=supports,
            reason=(
                f"{len(overlap)} of {len(q_terms)} question terms appear in {section_id}"
                + (f": {', '.join(sorted(overlap)[:6])}" if overlap else "")
            ),
            confidence=round(min(1.0, ratio), 3),
        )


STOPWORDS = frozenset("""
a an and are as at be by can do does for from has have how i in is it its of on or shall
the this to what when where which who why with you your must may not need required do
""".split())


def _content_terms(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z][a-z\-]{2,}", text.lower()) if t not in STOPWORDS
    }


# ---------------------------------------------------------------------------
# Hosted provider
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "value", "confidence", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}

SUPPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supports": {"type": "boolean"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["supports", "reason", "confidence"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM = """\
You extract structured fields from a building permit scope-of-work narrative written by an \
applicant. You are drafting for an intake clerk who will review every field before it is \
committed, so a blank is always better than a guess.

Extract only these field names:
  work_type            one of NEW_CONSTRUCTION, ADDITION, ALTERATION, TENANT_FITOUT,
                       ACCESSORY_DECK, ACCESSORY_STRUCTURE, DEMOLITION
  declared_valuation   digits only, no currency symbol or separators
  square_feet          digits only
  occupancy_class      an IBC/IRC occupancy group such as R-3, B, M, A-2
  discipline:ZONING | discipline:STRUCTURAL | discipline:FIRE | discipline:ENVIRONMENTAL
                       value "true" when the narrative describes work implicating that
                       discipline's review

Rules:
- Omit any field the narrative does not support. Do not infer from what is typical.
- evidence must be a verbatim span copied from the narrative.
- confidence is your calibrated probability the value is correct, between 0 and 1.
"""

SUPPORT_SYSTEM = """\
You judge whether one section of a municipal zoning ordinance answers a reviewer's \
question. You are given the section's exact text.

Answer only whether this section supports answering the question. Do not answer the \
question itself, do not quote the section, and do not supply any provision text. The \
system quotes the ordinance directly from its own corpus; your only job is the judgement.

Set supports to false when the section is merely on a related topic without addressing \
what was asked. An unanswered question is a correct outcome.
"""


@dataclass
class AnthropicProvider:
    """Hosted path, used when AI_PROVIDER=anthropic and a key is configured.

    Note what it is not asked to do. It never returns ordinance text, so a hallucinated
    provision cannot reach a reviewer. It returns a judgement and, for extraction, values
    it must evidence with a verbatim span from the applicant's own narrative.
    """

    name: str = "anthropic"
    model: str = "claude-opus-5"
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        try:
            import anthropic  # noqa: PLC0415 - optional dependency, imported on use
        except ImportError as exc:  # pragma: no cover - exercised only without the SDK
            raise RuntimeError(
                "AI_PROVIDER=anthropic requires the anthropic package: pip install anthropic"
            ) from exc
        self._client = anthropic.Anthropic()

    def _structured(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def extract_fields(self, narrative: str) -> list[FieldSuggestion]:
        data = self._structured(
            EXTRACTION_SYSTEM, f"<narrative>\n{narrative}\n</narrative>", EXTRACTION_SCHEMA
        )
        out: list[FieldSuggestion] = []
        for item in data.get("fields", []):
            out.append(
                FieldSuggestion(
                    name=item["name"],
                    value=_coerce(item["name"], item["value"]),
                    confidence=float(item["confidence"]),
                    evidence=item.get("evidence"),
                )
            )
        return out

    def assess_support(self, question: str, section_id: str, section_text: str) -> SupportVerdict:
        data = self._structured(
            SUPPORT_SYSTEM,
            f"<question>{question}</question>\n"
            f"<section id=\"{section_id}\">\n{section_text}\n</section>",
            SUPPORT_SCHEMA,
        )
        return SupportVerdict(
            supports=bool(data["supports"]),
            reason=str(data["reason"]),
            confidence=float(data["confidence"]),
        )


def _coerce(name: str, value: str) -> Any:
    """Bring model output back to the type the column expects."""
    if name.startswith("discipline:"):
        return str(value).strip().lower() in {"true", "yes", "1"}
    if name == "declared_valuation":
        return Decimal(re.sub(r"[^\d.]", "", str(value)) or "0")
    if name == "square_feet":
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None
    return value


def get_provider() -> Provider:
    """Build the configured provider, falling back to offline when unusable.

    The fallback is silent by design in one direction only: a missing key degrades to the
    deterministic path rather than failing intake. It never degrades the other way, since
    the offline path cannot produce anything ungrounded.
    """
    settings = get_settings()
    if settings.ai_provider == "anthropic":
        try:
            return AnthropicProvider()
        except Exception:  # noqa: BLE001 - any setup failure means use the offline path
            return OfflineProvider()
    return OfflineProvider()
