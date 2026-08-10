"""AI assistance endpoints.

Every route here returns a recommendation or a citation. None of them transitions a case,
which is AI-04 expressed as an API shape rather than as a promise: there is no endpoint in
this module that changes an application's status.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from ...ai import rag, recording, triage
from ...db import read_connection, transaction
from ...process.engine import Engine
from ..schemas import AiDecision
from ..security import CurrentActor

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/applications/{application_id}/triage")
def run_triage(application_id: UUID, actor: CurrentActor) -> dict:
    """Draft intake fields and recommend routing (AI-01, AI-02, AI-03, AI-07).

    Writes both recommendations as pending rows and returns them. Nothing is applied to
    the application until a clerk decides, and the decision is recorded against the same
    row so the audit trail names who chose what.
    """
    with transaction() as conn:
        app = Engine(conn).get_application(application_id)
        narrative = app["scope_narrative"]

        extraction = triage.extract(narrative)
        routing = triage.recommend_routing(conn, app["permit_type_code"], narrative)

        extraction_id = recording.record(
            conn,
            application_id=application_id,
            kind="field_extraction",
            payload=extraction.as_payload(),
            model=extraction.model,
            prompt_version=extraction.prompt_version,
            confidence=(
                round(
                    sum(s.confidence for s in extraction.accepted.values())
                    / len(extraction.accepted),
                    3,
                )
                if extraction.accepted
                else None
            ),
        )
        routing_id = recording.record(
            conn,
            application_id=application_id,
            kind="discipline_routing",
            payload=routing.as_payload(),
            model=routing.model,
            prompt_version=routing.prompt_version,
        )

        return {
            "field_extraction": {
                "recommendation_id": str(extraction_id),
                "accepted": {
                    name: {
                        "value": str(s.value),
                        "confidence": s.confidence,
                        "evidence": s.evidence,
                    }
                    for name, s in extraction.accepted.items()
                },
                "withheld_below_threshold": [
                    {"name": s.name, "confidence": s.confidence} for s in extraction.withheld
                ],
            },
            "discipline_routing": {
                "recommendation_id": str(routing_id),
                "always_required": routing.always_required,
                "recommended_additions": routing.recommended_additions,
                "reasoning": routing.reasoning,
            },
            "note": "Recommendations only. Nothing is applied until a clerk decides.",
        }


@router.get("/applications/{application_id}/recommendations")
def pending_recommendations(application_id: UUID, actor: CurrentActor) -> list[dict]:
    """Recommendations awaiting a human decision."""
    with read_connection() as conn:
        return recording.pending(conn, application_id)


@router.post("/recommendations/{recommendation_id}/decide")
def decide(recommendation_id: UUID, body: AiDecision, actor: CurrentActor) -> dict:
    """Accept or override a recommendation (AI-04, AI-07)."""
    with transaction() as conn:
        try:
            recording.decide(
                conn,
                recommendation_id,
                actor=actor.username,
                accepted=body.accepted,
                override_payload=body.override_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "recommendation_id": str(recommendation_id),
            "accepted": body.accepted,
            "decided_by": actor.username,
        }


@router.get("/ordinance")
def ask_ordinance(
    actor: CurrentActor,
    q: str = Query(min_length=5, description="A reviewer's question about the zoning ordinance"),
) -> dict:
    """Answer a zoning question with verbatim citations, or decline (AI-05, AI-06).

    Answers quote the ordinance directly. A question the corpus does not answer comes back
    as unanswerable with the sections that were considered, because an ungrounded answer to
    a code question is how a permit gets wrongly issued.
    """
    answer = rag.answer_question(q)
    return {
        "question": answer.question,
        "answered": answer.answered,
        "citations": [
            {
                "section": c.label,
                "quote": c.display_quote,
                "source": c.source,
                "relevance": c.relevance,
            }
            for c in answer.citations
        ],
        "withheld_reason": answer.withheld_reason,
        "sections_considered": answer.considered,
        "model": answer.model,
    }
