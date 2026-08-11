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

#: The council standards from `sql/003_seed.sql`. The configuration screen can change these
#: and the change is retroactive, so a reset that did not restore them would leave the
#: compliance figure quietly measured against whatever a test last set.
SEEDED_COUNCIL_STANDARDS = {
    "BLD-RES-NEW": 20,
    "BLD-RES-ALT": 20,
    "BLD-RES-ACC": 15,
    "BLD-COM-NEW": 30,
    "BLD-COM-ALT": 30,
}

#: The phase allowances from the same file, keyed by permit type, phase, and discipline.
#: They sum to the council standard above, which is the arithmetic the seed comment states
#: and the configuration screen flags when an edit breaks it.
SEEDED_ALLOWANCES = {
    ("BLD-RES-NEW", "INTAKE_SCREENING", None): 3,
    ("BLD-RES-NEW", "REVIEW_TASK", None): 14,
    ("BLD-RES-NEW", "PENDING_DECISION", None): 3,
    ("BLD-RES-ALT", "INTAKE_SCREENING", None): 3,
    ("BLD-RES-ALT", "REVIEW_TASK", None): 14,
    ("BLD-RES-ALT", "PENDING_DECISION", None): 3,
    ("BLD-RES-ACC", "INTAKE_SCREENING", None): 2,
    ("BLD-RES-ACC", "REVIEW_TASK", None): 11,
    ("BLD-RES-ACC", "PENDING_DECISION", None): 2,
    ("BLD-COM-NEW", "INTAKE_SCREENING", None): 4,
    ("BLD-COM-NEW", "REVIEW_TASK", None): 22,
    ("BLD-COM-NEW", "PENDING_DECISION", None): 4,
    ("BLD-COM-ALT", "INTAKE_SCREENING", None): 4,
    ("BLD-COM-ALT", "REVIEW_TASK", None): 22,
    ("BLD-COM-ALT", "PENDING_DECISION", None): 4,
    ("BLD-COM-NEW", "REVIEW_TASK", "ENVIRONMENTAL"): 25,
}

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

        # Allowances and council standards the configuration screen can edit. Restoring
        # them matters more than it looks: the compliance view applies the current council
        # standard to every case ever decided, so a standard left where a test put it would
        # silently change what the next run reports.
        for code, days in SEEDED_COUNCIL_STANDARDS.items():
            cur.execute(
                "UPDATE permit_type SET council_standard_days = %s WHERE code = %s", (days, code)
            )
        for (permit_type, phase, discipline), days in SEEDED_ALLOWANCES.items():
            cur.execute(
                """UPDATE sla_policy SET allowance_days = %s
                   WHERE permit_type_code = %s AND phase = %s
                     AND discipline_code IS NOT DISTINCT FROM %s""",
                (days, permit_type, phase, discipline),
            )


def case_row_count(conn: psycopg.Connection) -> int:
    """How many applications exist. Used to refuse a seed that would collide."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM application")
        return cur.fetchone()["n"]
