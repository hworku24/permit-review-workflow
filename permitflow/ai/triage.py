"""Intake triage: field extraction and discipline routing recommendations.

Two features, one rule. AI-01 drafts structured fields from the applicant's scope-of-work
narrative so the clerk corrects a draft instead of typing from scratch. AI-03 recommends
which disciplines the work implicates. Neither ever moves the case: everything here
returns a recommendation that a named human accepts or overrides, which is AI-04.

AI-02 is the part worth defending. A field that scores below the configured threshold is
returned as withheld rather than filled in. A blank field costs the clerk the twenty
seconds it would have taken anyway. A confidently wrong one gets accepted at a glance and
becomes the record, and nobody looks at the narrative again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

from ..config import get_settings
from ..process import routing
from .provider import PROMPT_VERSION, FieldSuggestion, Provider, get_provider

#: Fields the clerk keys in at intake. Anything a provider returns outside this set is
#: dropped, so a model cannot invent a field name that later fails a column write.
EXTRACTABLE_FIELDS = frozenset(
    {"work_type", "declared_valuation", "square_feet", "occupancy_class"}
)


@dataclass(frozen=True)
class FieldExtraction:
    """What the clerk sees on the intake screen."""

    accepted: dict[str, FieldSuggestion] = field(default_factory=dict)
    withheld: list[FieldSuggestion] = field(default_factory=list)
    model: str = "offline-rules-v1"
    prompt_version: str = PROMPT_VERSION

    def as_payload(self) -> dict[str, Any]:
        """Serializable form for the audit record (AI-07)."""
        return {
            "accepted": {
                name: {
                    "value": _jsonable(s.value),
                    "confidence": s.confidence,
                    "evidence": s.evidence,
                }
                for name, s in self.accepted.items()
            },
            "withheld": [
                {
                    "name": s.name,
                    "value": _jsonable(s.value),
                    "confidence": s.confidence,
                    "evidence": s.evidence,
                }
                for s in self.withheld
            ],
        }


@dataclass(frozen=True)
class RoutingRecommendation:
    """Standing rules plus whatever the narrative implicates."""

    always_required: list[str]
    recommended_additions: list[str]
    reasoning: dict[str, str]
    model: str = "offline-rules-v1"
    prompt_version: str = PROMPT_VERSION

    @property
    def proposed(self) -> list[str]:
        return sorted(set(self.always_required) | set(self.recommended_additions))

    def as_payload(self) -> dict[str, Any]:
        return {
            "always_required": self.always_required,
            "recommended_additions": self.recommended_additions,
            "reasoning": self.reasoning,
        }


def extract(narrative: str, provider: Provider | None = None) -> FieldExtraction:
    """Draft intake fields from the narrative (AI-01, AI-02)."""
    provider = provider or get_provider()
    threshold = get_settings().ai_field_confidence_threshold

    accepted: dict[str, FieldSuggestion] = {}
    withheld: list[FieldSuggestion] = []

    for suggestion in provider.extract_fields(narrative):
        if suggestion.name not in EXTRACTABLE_FIELDS:
            continue  # discipline signals are handled by recommend_routing
        if suggestion.value is None:
            continue
        if suggestion.confidence >= threshold:
            accepted[suggestion.name] = suggestion
        else:
            withheld.append(suggestion)

    return FieldExtraction(
        accepted=accepted,
        withheld=withheld,
        model=provider.model,
        prompt_version=PROMPT_VERSION,
    )


def recommend_routing(
    conn: psycopg.Connection,
    permit_type_code: str,
    narrative: str,
    provider: Provider | None = None,
) -> RoutingRecommendation:
    """Recommend discipline routing with the reasoning shown (AI-03).

    The recommendation can only widen the routing. Standing rules come from
    `permit_type_discipline` and are added unconditionally, and an addition that is not a
    configured candidate for this permit type is dropped. A model that misses a stormwater
    implication therefore costs nothing that the department was not already going to miss,
    and a model that hallucinates a discipline changes nothing at all.
    """
    provider = provider or get_provider()
    threshold = get_settings().ai_field_confidence_threshold

    base = routing.always_required_disciplines(conn, permit_type_code)
    candidates = set(routing.candidate_disciplines(conn, permit_type_code))

    additions: list[str] = []
    reasoning: dict[str, str] = {}

    for suggestion in provider.extract_fields(narrative):
        if not suggestion.name.startswith("discipline:"):
            continue
        discipline = suggestion.name.split(":", 1)[1]
        if discipline in base or discipline not in candidates:
            continue
        if suggestion.confidence < threshold:
            continue
        additions.append(discipline)
        reasoning[discipline] = (
            f"narrative mentions \"{suggestion.evidence}\" "
            f"(confidence {suggestion.confidence:.2f})"
        )

    for discipline in base:
        reasoning.setdefault(
            discipline, f"standing rule for permit type {permit_type_code}"
        )

    return RoutingRecommendation(
        always_required=base,
        recommended_additions=sorted(set(additions)),
        reasoning=reasoning,
        model=provider.model,
        prompt_version=PROMPT_VERSION,
    )


def _jsonable(value: Any) -> Any:
    """Decimal and friends into something jsonb will take."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
