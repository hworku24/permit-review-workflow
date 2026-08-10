"""Audit trail writer.

FR-20 wants every transition, assignment, document action, integration call, and AI
recommendation recorded with actor, timestamp, before, and after. FR-21 wants those
records to be unalterable, which is enforced by triggers in sql/001_schema.sql rather
than here. This module only writes.

The reason the department asked for this is in the discovery notes as P6: when the 2025
Aldergate denial was appealed, nobody could reconstruct who changed the zoning
determination or when.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


def record(
    conn: psycopg.Connection,
    *,
    entity_type: str,
    entity_id: UUID | str | None,
    action: str,
    actor: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append one audit row.

    Called inside the caller's transaction on purpose. An audit row that commits while
    the change it describes rolls back would be worse than no audit row at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (entity_type, entity_id, action, actor, before_value, after_value)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                entity_type,
                str(entity_id) if entity_id is not None else None,
                action,
                actor,
                Jsonb(before) if before is not None else None,
                Jsonb(after) if after is not None else None,
            ),
        )


def history(conn: psycopg.Connection, entity_type: str, entity_id: UUID | str) -> list[dict[str, Any]]:
    """Every audit row for one entity, oldest first. Backs US-16."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_type, entity_id, action, actor, before_value, after_value, occurred_at
            FROM audit_log
            WHERE entity_type = %s AND entity_id = %s
            ORDER BY occurred_at, id
            """,
            (entity_type, str(entity_id)),
        )
        return cur.fetchall()
