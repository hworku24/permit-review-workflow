"""Required document checklist derivation (FR-04).

The rules live in `permit_type_document`, so adding a required document is a row rather
than a release. `min_valuation` gates a document behind a project size, which is how the
department avoids demanding sealed structural calculations for a rear deck.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import psycopg


def required_document_codes(
    conn: psycopg.Connection, permit_type_code: str, declared_valuation: Decimal | float
) -> list[str]:
    """Document codes this application must supply, in a stable order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document_type_code
            FROM permit_type_document
            WHERE permit_type_code = %s
              AND required
              AND (min_valuation IS NULL OR %s >= min_valuation)
            ORDER BY document_type_code
            """,
            (permit_type_code, Decimal(str(declared_valuation))),
        )
        return [r["document_type_code"] for r in cur.fetchall()]


def build_checklist(conn: psycopg.Connection, application_id: UUID) -> list[str]:
    """Create MISSING rows for every required document not already on the application.

    Idempotent, because it runs again on every resubmission and a resubmission that
    raised the declared valuation can pull in a document the first pass did not require.
    Existing rows are left alone so an uploaded document is never reset to MISSING.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT permit_type_code, declared_valuation FROM application WHERE id = %s",
            (str(application_id),),
        )
        app = cur.fetchone()

    required = required_document_codes(conn, app["permit_type_code"], app["declared_valuation"])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document_type_code FROM application_document
            WHERE application_id = %s AND status <> 'SUPERSEDED'
            """,
            (str(application_id),),
        )
        existing = {r["document_type_code"] for r in cur.fetchall()}

        for code in required:
            if code not in existing:
                cur.execute(
                    """
                    INSERT INTO application_document (application_id, document_type_code, status)
                    VALUES (%s, %s, 'MISSING')
                    """,
                    (str(application_id), code),
                )

    return required


def missing_document_codes(conn: psycopg.Connection, application_id: UUID) -> list[str]:
    """Required documents still outstanding. Blocks accept_intake per FR-04.

    A WAIVED document counts as satisfied. Waivers carry an owner and a reason, enforced
    by a check constraint, so waiving is a recorded decision rather than a way around the
    checklist.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document_type_code
            FROM application_document
            WHERE application_id = %s AND status = 'MISSING'
            ORDER BY document_type_code
            """,
            (str(application_id),),
        )
        return [r["document_type_code"] for r in cur.fetchall()]
