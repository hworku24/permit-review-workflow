"""Discipline routing (FR-07).

Two sources feed the routing decision. `permit_type_discipline.always_required` is the
department's standing rule and routes unconditionally. Everything else is a candidate that
the triage step in `permitflow/ai/triage.py` may recommend based on the scope of work, and
that an intake clerk confirms before any task is created.

The split matters for AI-04. The model can widen the routing but it cannot narrow it, so a
model that fails to notice a stormwater implication still leaves the standing rules intact.
"""

from __future__ import annotations

import psycopg


def always_required_disciplines(conn: psycopg.Connection, permit_type_code: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT discipline_code FROM permit_type_discipline
            WHERE permit_type_code = %s AND always_required
            ORDER BY discipline_code
            """,
            (permit_type_code,),
        )
        return [r["discipline_code"] for r in cur.fetchall()]


def candidate_disciplines(conn: psycopg.Connection, permit_type_code: str) -> list[str]:
    """Disciplines that may apply to this permit type but are not automatic."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT discipline_code FROM permit_type_discipline
            WHERE permit_type_code = %s AND NOT always_required
            ORDER BY discipline_code
            """,
            (permit_type_code,),
        )
        return [r["discipline_code"] for r in cur.fetchall()]


def resolve_routing(
    conn: psycopg.Connection, permit_type_code: str, confirmed_additions: list[str] | None = None
) -> list[str]:
    """Final discipline list for an application.

    Standing rules plus whatever additions a human confirmed. An addition that is not a
    configured candidate for this permit type is dropped rather than honoured, so a bad
    recommendation cannot invent a discipline the department does not run for this work.
    """
    base = always_required_disciplines(conn, permit_type_code)
    if not confirmed_additions:
        return base

    allowed = set(candidate_disciplines(conn, permit_type_code))
    additions = [d for d in confirmed_additions if d in allowed and d not in base]
    return sorted(set(base) | set(additions))
