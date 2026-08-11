"""Persisting AI recommendations and the human decisions on them (AI-07).

Every recommendation is written before a human sees it, and the accept-or-override is
written back against the same row. That pairing is the point: a year later, when someone
asks why an application was routed to environmental review, the answer is a row naming the
model, the prompt version, the suggestion, the clerk, and whether they took it.

`accepted` starts null rather than false. Null means nobody has decided yet, which is a
different state from a human having rejected the suggestion, and collapsing the two would
make the audit trail lie about who chose what.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .. import audit


def record(
    conn: psycopg.Connection,
    *,
    application_id: UUID,
    kind: str,
    payload: dict[str, Any],
    model: str,
    prompt_version: str,
    confidence: float | None = None,
    sources: list[dict[str, Any]] | None = None,
    occurred_at: datetime | None = None,
) -> UUID:
    """Store a recommendation awaiting a human decision."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_recommendation (
                application_id, kind, model, prompt_version, payload, confidence, sources,
                created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
            RETURNING id
            """,
            (
                str(application_id),
                kind,
                model,
                prompt_version,
                Jsonb(payload),
                confidence,
                Jsonb(sources) if sources is not None else None,
                occurred_at,
            ),
        )
        recommendation_id = cur.fetchone()["id"]

    audit.record(
        conn,
        entity_type="ai",
        entity_id=recommendation_id,
        action=f"recommend:{kind}",
        actor="system",
        after={"model": model, "prompt_version": prompt_version, "payload": payload},
        occurred_at=occurred_at,
    )
    return recommendation_id


def decide(
    conn: psycopg.Connection,
    recommendation_id: UUID,
    *,
    actor: str,
    accepted: bool,
    override_payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Record a human accepting or overriding a recommendation (AI-04, AI-07).

    Refuses to record a second decision on the same recommendation. Re-deciding would
    overwrite the first decision and its actor, and the first decision is the one that
    actually moved the case.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ai_recommendation
            SET accepted = %s,
                override_payload = %s,
                decided_by = %s,
                decided_at = COALESCE(%s, now())
            WHERE id = %s AND accepted IS NULL
            RETURNING kind, payload
            """,
            (
                accepted,
                Jsonb(override_payload) if override_payload is not None else None,
                actor,
                occurred_at,
                str(recommendation_id),
            ),
        )
        row = cur.fetchone()

    if row is None:
        raise ValueError(f"recommendation {recommendation_id} is unknown or already decided")

    audit.record(
        conn,
        entity_type="ai",
        entity_id=recommendation_id,
        action="accept" if accepted else "override",
        actor=actor,
        before={"suggested": row["payload"]},
        after={"accepted": accepted, "override": override_payload},
        occurred_at=occurred_at,
    )


def pending(conn: psycopg.Connection, application_id: UUID) -> list[dict[str, Any]]:
    """Recommendations on this application that no human has ruled on yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, model, prompt_version, payload, confidence, sources, created_at
            FROM ai_recommendation
            WHERE application_id = %s AND accepted IS NULL
            ORDER BY created_at
            """,
            (str(application_id),),
        )
        return cur.fetchall()
