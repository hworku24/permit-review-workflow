"""Reporting endpoints (FR-22, FR-23, FR-24).

Thin wrappers over the views. The arithmetic lives in SQL because the compliance number
goes to the city council and a number that depends on replaying an event log correctly in
application code is a number that will eventually be wrong in a way nobody notices.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...db import read_connection
from ..security import CurrentActor

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/sla-compliance")
def sla_compliance(actor: CurrentActor, permit_type_code: str | None = None) -> list[dict]:
    """Monthly compliance against the council standard, with median and p90 (FR-23)."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT decided_month, permit_type_code, permit_type_name, council_standard_days,
                   decided_count, met_count, compliance_pct,
                   mean_net_days, median_net_days, p90_net_days
            FROM v_sla_compliance
            WHERE (%s IS NULL OR permit_type_code = %s)
            ORDER BY decided_month DESC, permit_type_code
            """,
            (permit_type_code, permit_type_code),
        )
        return cur.fetchall()


@router.get("/cycle-time")
def cycle_time(actor: CurrentActor, limit: int = Query(default=100, le=1000)) -> list[dict]:
    """Per-application durations (FR-22).

    Reports gross, net, and applicant wait separately. Gross minus wait equals net, and
    showing all three is what lets the department answer "why was this one slow" without
    arguing about whose delay it was.
    """
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT application_number, permit_type_code, status, decided_at,
                   gross_business_days, net_business_days, applicant_wait_business_days,
                   review_rounds
            FROM v_cycle_time
            ORDER BY decided_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


@router.get("/open-work")
def open_work(actor: CurrentActor) -> dict:
    """Everything currently in flight, grouped by state and SLA position."""
    with read_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM v_application_summary
                WHERE status NOT IN ('ISSUED', 'DENIED', 'WITHDRAWN', 'EXPIRED', 'APPEAL_FILED')
                GROUP BY status ORDER BY status
                """
            )
            by_status = cur.fetchall()

            cur.execute(
                """
                SELECT sla_state, COUNT(*) AS count
                FROM v_sla_status
                WHERE status NOT IN ('ISSUED', 'DENIED', 'WITHDRAWN', 'EXPIRED', 'APPEAL_FILED')
                GROUP BY sla_state ORDER BY sla_state
                """
            )
            by_sla = cur.fetchall()

        return {"by_status": by_status, "by_sla_state": by_sla}
