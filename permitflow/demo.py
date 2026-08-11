"""Resetting case data for demos and tests.

Not part of the application surface. Nothing under `permitflow/api` or `permitflow/process`
imports this, and the role the application connects as cannot run what is in here. It lives
in the package so the test fixtures and the seeding scripts share one list of tables, since
a table added to the schema and forgotten in one of those two lists produces a reset that
silently leaves rows behind.
"""

from __future__ import annotations

import psycopg

#: The disciplines `sql/003_seed.sql` creates. Anything else in the table was added through
#: the configuration screen, by a demo or by a test, and a reset removes it. Without this a
#: test run leaves its invented disciplines behind and the next configuration screen shows
#: ACOUSTIC and ORPHAN next to the real four.
SEEDED_DISCIPLINES = ("ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL")

#: Truncated on reset, child tables first so `RESTART IDENTITY CASCADE` has nothing to
#: complain about. Reference data (permit types, disciplines, staff, holidays, SLA
#: policies, document types) is loaded once by docker-entrypoint and left alone, apart from
#: the restores at the bottom of `reset_case_data`.
CASE_TABLES = [
    "audit_log",
    "integration_call",
    "ai_recommendation",
    "escalation",
    "clock_pause",
    "status_history",
    "deficiency",
    "review_condition",
    "review_task",
    "application_document",
    "application",
    "applicant",
    "contractor",
    "parcel",
]


def reset_case_data(conn: psycopg.Connection) -> None:
    """Wipe every case row and restore reference data to its seeded state.

    Note what clearing `audit_log` costs. The append-only triggers reject DELETE and
    TRUNCATE, so this disables them explicitly as the table owner. Resetting the audit
    trail takes a privilege the application role never holds, which is what FR-21 asks for.

    The connection must be in autocommit. `ALTER TABLE ... DISABLE TRIGGER` takes an
    ACCESS EXCLUSIVE lock, and holding that inside a long transaction blocks every reader
    of the audit log until it commits.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        cur.execute(f"TRUNCATE {', '.join(CASE_TABLES)} RESTART IDENTITY CASCADE")
        cur.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
        # The application number sequence is standalone, not owned by a column, so
        # RESTART IDENTITY above does not reach it. Without this a second seeding run
        # produces the same cases under different numbers, and a demo script that names
        # BLD-2026-00041 stops matching what is on screen.
        cur.execute("ALTER SEQUENCE application_number_seq RESTART")
        # Reference data is not truncated, but a test or a demo is allowed to mutate it.
        # Restoring it here is what makes a reset reproduce one known state every time.
        cur.execute("UPDATE reviewer SET active = (username <> 'tbrandt')")

        # Disciplines added through the configuration screen, with the routing and
        # certification rows that hang off them. Deleted in dependency order, because the
        # foreign keys are there precisely so a discipline cannot be removed while
        # something still points at it.
        cur.execute(
            "DELETE FROM reviewer_discipline WHERE discipline_code <> ALL(%s)",
            (list(SEEDED_DISCIPLINES),),
        )
        cur.execute(
            "DELETE FROM permit_type_discipline WHERE discipline_code <> ALL(%s)",
            (list(SEEDED_DISCIPLINES),),
        )
        cur.execute(
            "DELETE FROM sla_policy WHERE discipline_code IS NOT NULL AND discipline_code <> ALL(%s)",
            (list(SEEDED_DISCIPLINES),),
        )
        cur.execute("DELETE FROM discipline WHERE code <> ALL(%s)", (list(SEEDED_DISCIPLINES),))


def case_row_count(conn: psycopg.Connection) -> int:
    """How many applications exist. Used to refuse a seed that would collide."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM application")
        return cur.fetchone()["n"]
