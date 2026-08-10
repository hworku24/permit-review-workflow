"""Escalation sweep (FR-16, FR-17).

Runs on a schedule. Reads `v_task_sla_status`, which already knows how to count business
days, and raises a WARNING when a task passes its warning threshold and a BREACH when it
passes its allowance.

Two levels exist for a practical reason. Discovery pain point P3 was that work sat in
queues nobody was watching for a median of six days. A supervisor who only hears about a
breach at the moment it happens has lost the chance to do anything about it, so the
warning is the part of this that actually changes an outcome.

The sweep is safe to run as often as you like. Duplicate suppression is a unique index in
the schema rather than a check in this code, so two overlapping runs cannot both insert.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .. import audit


@dataclass(frozen=True)
class SweepResult:
    warnings_raised: int
    breaches_raised: int

    @property
    def total(self) -> int:
        return self.warnings_raised + self.breaches_raised


def sweep(conn: psycopg.Connection) -> SweepResult:
    """Raise escalations for every open task past its threshold."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT review_task_id, application_id, application_number,
                   discipline_code, reviewer_name, sla_state,
                   business_days_open, allowance_days
            FROM v_task_sla_status
            WHERE sla_state IN ('AT_RISK', 'BREACHED')
            """
        )
        rows = cur.fetchall()

    warnings = 0
    breaches = 0

    for row in rows:
        level = "BREACH" if row["sla_state"] == "BREACHED" else "WARNING"
        reason = (
            f"{row['discipline_code']} review on {row['application_number']} is at "
            f"{row['business_days_open']} of {row['allowance_days']} business days"
        )
        if row["reviewer_name"]:
            reason += f", assigned to {row['reviewer_name']}"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO escalation (application_id, review_task_id, level, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (str(row["application_id"]), str(row["review_task_id"]), level, reason),
            )
            inserted = cur.fetchone()

        if inserted is None:
            # Already escalated at this level. Expected on every run after the first.
            continue

        audit.record(
            conn,
            entity_type="escalation",
            entity_id=inserted["id"],
            action=f"raise:{level}",
            actor="system",
            after={"review_task_id": str(row["review_task_id"]), "reason": reason},
        )

        if level == "BREACH":
            breaches += 1
        else:
            warnings += 1

    return SweepResult(warnings_raised=warnings, breaches_raised=breaches)


def acknowledge(conn: psycopg.Connection, escalation_id, actor_username: str) -> None:
    """Supervisor acknowledges an escalation, removing it from the open queue."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE escalation
            SET acknowledged_at = now(), acknowledged_by = %s
            WHERE id = %s AND acknowledged_at IS NULL
            """,
            (actor_username, str(escalation_id)),
        )
    audit.record(
        conn,
        entity_type="escalation",
        entity_id=escalation_id,
        action="acknowledge",
        actor=actor_username,
    )
