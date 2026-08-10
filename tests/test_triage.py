"""Intake triage (AI-01 to AI-04, AI-07).

The load-bearing property is AI-04: nothing in the AI layer can move a case. Everything
else here is about a recommendation being reviewable, which means an evidence span, a
calibrated confidence, and a recorded human decision.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from permitflow.ai import recording, triage
from permitflow.ai.provider import FieldSuggestion, OfflineProvider
from permitflow.config import get_settings
from permitflow.process.engine import Engine

from .conftest import APPLICANT, CLERK

RESIDENTIAL = (
    "Two story rear addition to an existing single-family home, 640 sq ft of new "
    "conditioned space. New load-bearing beam at the rear wall and a foundation "
    "extension. Declared value $180,000."
)
COMMERCIAL = (
    "Tenant fit-out of a 4,200 square foot retail suite for a restaurant. New commercial "
    "kitchen with a suppression hood, revised egress, and sprinkler relocation. "
    "Valuation $315,000. Occupancy class A-2."
)
ENVIRONMENTAL = (
    "New single family home on a wooded lot. Clearing and grading, adds 6,200 sq ft of "
    "impervious area and a stormwater facility. Valuation $720,000."
)


@pytest.fixture
def provider() -> OfflineProvider:
    return OfflineProvider()


class TestExtraction:
    def test_extraction_shape(self, provider) -> None:
        """AI-01."""
        result = triage.extract(RESIDENTIAL, provider=provider)
        assert result.accepted["work_type"].value == "ADDITION"
        assert result.accepted["square_feet"].value == 640
        assert result.accepted["declared_valuation"].value == Decimal("180000")
        assert result.accepted["occupancy_class"].value == "R-3"

    def test_every_field_carries_evidence(self, provider) -> None:
        """A clerk checks the suggestion against the source rather than rereading."""
        result = triage.extract(RESIDENTIAL, provider=provider)
        for suggestion in result.accepted.values():
            assert suggestion.evidence
            assert suggestion.evidence.lower() in RESIDENTIAL.lower()

    def test_explicit_occupancy_beats_inference(self, provider) -> None:
        result = triage.extract(COMMERCIAL, provider=provider)
        assert result.accepted["occupancy_class"].value == "A-2"
        assert result.accepted["occupancy_class"].confidence > 0.9

    def test_nothing_extracted_from_a_bare_narrative(self, provider) -> None:
        result = triage.extract("Replace the back fence.", provider=provider)
        assert result.accepted == {}

    def test_low_confidence_left_blank(self) -> None:
        """AI-02: a blank costs twenty seconds, a confident wrong value becomes the record."""

        class LowConfidence(OfflineProvider):
            def extract_fields(self, narrative):
                return [FieldSuggestion("square_feet", 640, 0.31, "640 sq ft")]

        result = triage.extract(RESIDENTIAL, provider=LowConfidence())
        assert "square_feet" not in result.accepted
        assert [s.name for s in result.withheld] == ["square_feet"]

    def test_threshold_is_configuration(self) -> None:
        threshold = get_settings().ai_field_confidence_threshold

        class AtThreshold(OfflineProvider):
            def extract_fields(self, narrative):
                return [
                    FieldSuggestion("square_feet", 1, threshold, "at"),
                    FieldSuggestion("occupancy_class", "R-3", threshold - 0.01, "below"),
                ]

        result = triage.extract("x", provider=AtThreshold())
        assert "square_feet" in result.accepted
        assert [s.name for s in result.withheld] == ["occupancy_class"]

    def test_unknown_field_names_are_dropped(self) -> None:
        """A model cannot invent a field that would later fail a column write."""

        class Inventive(OfflineProvider):
            def extract_fields(self, narrative):
                return [FieldSuggestion("applicant_credit_score", 700, 0.99, "made up")]

        result = triage.extract("x", provider=Inventive())
        assert result.accepted == {} and result.withheld == []

    def test_payload_is_serializable(self, provider) -> None:
        payload = triage.extract(RESIDENTIAL, provider=provider).as_payload()
        assert isinstance(payload["accepted"]["declared_valuation"]["value"], str)


class TestRoutingRecommendation:
    def test_routing_recommendation(self, conn, provider) -> None:
        """AI-03."""
        result = triage.recommend_routing(conn, "BLD-RES-NEW", ENVIRONMENTAL, provider=provider)
        assert result.always_required == ["STRUCTURAL", "ZONING"]
        assert result.recommended_additions == ["ENVIRONMENTAL"]
        assert "stormwater" in result.reasoning["ENVIRONMENTAL"]

    def test_reasoning_covers_standing_rules_too(self, conn, provider) -> None:
        result = triage.recommend_routing(conn, "BLD-RES-ALT", RESIDENTIAL, provider=provider)
        for discipline in result.always_required:
            assert discipline in result.reasoning

    def test_no_signal_means_no_addition(self, conn, provider) -> None:
        result = triage.recommend_routing(
            conn, "BLD-RES-ALT", "Replace kitchen cabinets.", provider=provider
        )
        assert result.recommended_additions == []
        assert result.proposed == result.always_required

    def test_hallucinated_discipline_is_dropped(self, conn) -> None:
        """A recommendation cannot invent a discipline the department does not run."""

        class Rogue(OfflineProvider):
            def extract_fields(self, narrative):
                return [FieldSuggestion("discipline:HISTORIC", True, 0.99, "invented")]

        result = triage.recommend_routing(conn, "BLD-RES-ALT", "x", provider=Rogue())
        assert "HISTORIC" not in result.proposed
        assert result.proposed == result.always_required

    def test_recommendation_can_only_widen(self, conn) -> None:
        """A model that notices nothing still leaves the standing rules intact."""

        class Silent(OfflineProvider):
            def extract_fields(self, narrative):
                return []

        result = triage.recommend_routing(conn, "BLD-COM-NEW", "x", provider=Silent())
        assert set(result.proposed) == {"ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL"}

    def test_low_confidence_signal_is_ignored(self, conn) -> None:
        class Unsure(OfflineProvider):
            def extract_fields(self, narrative):
                return [FieldSuggestion("discipline:ENVIRONMENTAL", True, 0.2, "tree")]

        result = triage.recommend_routing(conn, "BLD-RES-ALT", "x", provider=Unsure())
        assert result.recommended_additions == []


class TestAiCannotDecide:
    def test_ai_cannot_transition(self, conn, engine: Engine, make_application, provider) -> None:
        """AI-04: no AI output causes a state change."""
        application_id = make_application()
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        before = engine.get_application(application_id)["status"]

        triage.extract(RESIDENTIAL, provider=provider)
        triage.recommend_routing(conn, "BLD-RES-ALT", RESIDENTIAL, provider=provider)

        assert engine.get_application(application_id)["status"] == before

    def test_recommendation_starts_undecided(self, conn, make_application, provider) -> None:
        """Null is a different state from a human having rejected the suggestion."""
        application_id = make_application()
        extraction = triage.extract(RESIDENTIAL, provider=provider)
        recommendation_id = recording.record(
            conn,
            application_id=application_id,
            kind="field_extraction",
            payload=extraction.as_payload(),
            model=extraction.model,
            prompt_version=extraction.prompt_version,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT accepted, decided_by, decided_at FROM ai_recommendation WHERE id=%s",
                (str(recommendation_id),),
            )
            row = cur.fetchone()
        assert row["accepted"] is None
        assert row["decided_by"] is None and row["decided_at"] is None


class TestRecording:
    def _record(self, conn, application_id, provider):
        extraction = triage.extract(RESIDENTIAL, provider=provider)
        return recording.record(
            conn,
            application_id=application_id,
            kind="field_extraction",
            payload=extraction.as_payload(),
            model=extraction.model,
            prompt_version=extraction.prompt_version,
            confidence=0.88,
        )

    def test_recommendation_recorded(self, conn, make_application, provider) -> None:
        """AI-07: model, prompt version, and payload."""
        application_id = make_application()
        recommendation_id = self._record(conn, application_id, provider)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT model, prompt_version, payload, confidence FROM ai_recommendation WHERE id=%s",
                (str(recommendation_id),),
            )
            row = cur.fetchone()
        assert row["model"] and row["prompt_version"]
        assert "accepted" in row["payload"]

        from permitflow import audit

        actions = {r["action"] for r in audit.history(conn, "ai", recommendation_id)}
        assert "recommend:field_extraction" in actions

    def test_decision_is_recorded_with_actor(self, conn, make_application, provider) -> None:
        application_id = make_application()
        recommendation_id = self._record(conn, application_id, provider)
        recording.decide(conn, recommendation_id, actor=CLERK.username, accepted=True)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT accepted, decided_by FROM ai_recommendation WHERE id=%s",
                (str(recommendation_id),),
            )
            row = cur.fetchone()
        assert row["accepted"] is True
        assert row["decided_by"] == CLERK.username

    def test_override_payload_is_kept(self, conn, make_application, provider) -> None:
        application_id = make_application()
        recommendation_id = self._record(conn, application_id, provider)
        recording.decide(
            conn,
            recommendation_id,
            actor=CLERK.username,
            accepted=False,
            override_payload={"square_feet": 720},
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT accepted, override_payload FROM ai_recommendation WHERE id=%s",
                (str(recommendation_id),),
            )
            row = cur.fetchone()
        assert row["accepted"] is False
        assert row["override_payload"] == {"square_feet": 720}

    def test_second_decision_refused(self, conn, make_application, provider) -> None:
        """The first decision is the one that moved the case."""
        application_id = make_application()
        recommendation_id = self._record(conn, application_id, provider)
        recording.decide(conn, recommendation_id, actor=CLERK.username, accepted=True)
        with pytest.raises(ValueError, match="already decided"):
            recording.decide(conn, recommendation_id, actor="jtakeda", accepted=False)

    def test_pending_excludes_decided(self, conn, make_application, provider) -> None:
        application_id = make_application()
        recommendation_id = self._record(conn, application_id, provider)
        assert len(recording.pending(conn, application_id)) == 1
        recording.decide(conn, recommendation_id, actor=CLERK.username, accepted=True)
        assert recording.pending(conn, application_id) == []
