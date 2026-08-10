"""Work queues and the supervisor escalation list."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from ...db import read_connection, transaction
from ...process import escalation
from ...process.states import Role
from ..security import CurrentActor, require_roles

router = APIRouter(prefix="/queues", tags=["queues"])


@router.get("/intake")
def intake_queue(
    actor=Depends(require_roles(Role.INTAKE_CLERK, Role.SUPERVISOR)),
) -> list[dict]:
    """Applications waiting on screening, oldest first.

    Surfaces the unverified flags so a clerk sees immediately that the county lookup or
    the licence check did not complete, which is what NFR-02 means in practice.
    """
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT application_id, application_number, permit_type_name, applicant_name,
                   situs_address, missing_document_count, parcel_verified, license_verified,
                   status_since
            FROM v_application_summary
            WHERE status = 'INTAKE_SCREENING'
            ORDER BY status_since
            """
        )
        return cur.fetchall()


@router.get("/mine")
def my_queue(actor=Depends(require_roles(Role.REVIEWER))) -> list[dict]:
    """A reviewer's open tasks, most at-risk first."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.review_task_id, t.application_number, t.discipline_code, t.round,
                   t.status, t.business_days_open, t.allowance_days, t.sla_state, t.sla_due_at,
                   s.situs_address, s.permit_type_name
            FROM v_task_sla_status t
            JOIN v_application_summary s ON s.application_id = t.application_id
            WHERE t.reviewer_username = %s
              AND t.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
            ORDER BY CASE t.sla_state
                        WHEN 'BREACHED' THEN 0 WHEN 'AT_RISK' THEN 1 ELSE 2 END,
                     t.sla_due_at NULLS LAST
            """,
            (actor.username,),
        )
        return cur.fetchall()


@router.get("/escalations")
def open_escalations(actor=Depends(require_roles(Role.SUPERVISOR))) -> list[dict]:
    """Unacknowledged escalations, breaches first then by time past due (FR-17)."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM v_open_escalations")
        return cur.fetchall()


@router.post("/escalations/{escalation_id}/acknowledge")
def acknowledge(
    escalation_id: UUID, actor=Depends(require_roles(Role.SUPERVISOR))
) -> dict:
    with transaction() as conn:
        escalation.acknowledge(conn, escalation_id, actor.username)
        return {"escalation_id": str(escalation_id), "acknowledged_by": actor.username}


@router.post("/escalations/sweep")
def sweep(actor=Depends(require_roles(Role.SUPERVISOR))) -> dict:
    """Run the SLA sweep now (FR-16).

    Exposed so the behaviour is demonstrable without waiting for the scheduler. In a
    deployment this runs on a timer; the duplicate suppression is a unique index, so
    running it by hand alongside the scheduler is safe.
    """
    with transaction() as conn:
        result = escalation.sweep(conn)
        return {
            "warnings_raised": result.warnings_raised,
            "breaches_raised": result.breaches_raised,
        }


@router.get("/workload")
def workload(actor: CurrentActor) -> list[dict]:
    """Open work per reviewer and discipline (FR-24). Answers discovery pain point P4."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT username, full_name, discipline_code, active, open_tasks,
                   completed_last_30_days, oldest_open_business_days
            FROM v_reviewer_workload
            ORDER BY open_tasks DESC, username
            """
        )
        return cur.fetchall()
