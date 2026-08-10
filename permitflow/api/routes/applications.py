"""Application lifecycle endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from ... import audit
from ...db import read_connection, transaction
from ...errors import NotFound
from ...integrations.licensing import IntakeEffect, verify_and_record
from ...integrations.property_records import enrich_parcel
from ...process import checklist
from ...process.engine import Engine
from ..schemas import (
    AcceptIntake,
    ApplicationCreated,
    ApplicationView,
    CreateApplication,
    Deny,
    DocumentReceived,
    ReturnIncomplete,
    WaiveDocument,
    Withdraw,
)
from ..security import CurrentActor

router = APIRouter(prefix="/applications", tags=["applications"])


@router.post("", response_model=ApplicationCreated, status_code=status.HTTP_201_CREATED)
def create_application(body: CreateApplication, actor: CurrentActor) -> ApplicationCreated:
    """Create a DRAFT application (FR-01). The SLA clock does not start here."""
    with transaction() as conn:
        engine = Engine(conn)
        application_id = engine.create_application(
            applicant_id=body.applicant_id,
            parcel_id=body.parcel_id,
            contractor_id=body.contractor_id,
            permit_type_code=body.permit_type_code,
            scope_narrative=body.scope_narrative,
            declared_valuation=body.declared_valuation,
            square_feet=body.square_feet,
            occupancy_class=body.occupancy_class,
            actor=actor,
        )
        app = engine.get_application(application_id)
        return ApplicationCreated(
            application_id=application_id,
            application_number=app["application_number"],
            status=app["status"],
        )


@router.post("/{application_id}/submit")
def submit(application_id: UUID, actor: CurrentActor) -> dict:
    """Submit, enrich from both external systems, and land on the intake queue.

    The two integration calls happen here rather than in the engine on purpose. The engine
    owns the lifecycle; reaching across the network is a different concern with a different
    failure mode, and NFR-02 requires that failure to degrade rather than block. Both
    lookups can fail and the application is still accepted, with the gap recorded.
    """
    with transaction() as conn:
        engine = Engine(conn)
        engine.submit(application_id, actor)
        app = engine.get_application(application_id)

        with conn.cursor() as cur:
            cur.execute("SELECT apn FROM parcel WHERE id = %s", (str(app["parcel_id"]),))
            apn = cur.fetchone()["apn"]

        parcel_outcome = enrich_parcel(
            conn, app["parcel_id"], apn, application_id=application_id
        )

        license_note = None
        if app["contractor_id"]:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT license_number FROM contractor WHERE id = %s",
                    (str(app["contractor_id"]),),
                )
                license_number = cur.fetchone()["license_number"]

            license_status, effect, license_note = verify_and_record(
                conn,
                app["contractor_id"],
                license_number,
                application_id=application_id,
            )
            if effect is IntakeEffect.ESCALATE_TO_SUPERVISOR:
                engine.escalate(application_id, license_note)

        engine.complete_enrichment(application_id)

        return {
            "status": engine.get_application(application_id)["status"],
            "parcel_verified": parcel_outcome.verified,
            "parcel_detail": parcel_outcome.detail,
            "license_note": license_note,
            "required_documents": checklist.missing_document_codes(conn, application_id),
        }


@router.post("/{application_id}/intake/return")
def return_incomplete(application_id: UUID, body: ReturnIncomplete, actor: CurrentActor) -> dict:
    """Return to the applicant with itemized reasons (FR-05). Pauses the clock."""
    with transaction() as conn:
        new_status = Engine(conn).return_incomplete(application_id, actor, body.reasons)
        return {"status": new_status.value, "reasons": body.reasons}


@router.post("/{application_id}/intake/accept")
def accept_intake(application_id: UUID, body: AcceptIntake, actor: CurrentActor) -> dict:
    """Open the discipline reviews (FR-04, FR-07, FR-09)."""
    with transaction() as conn:
        engine = Engine(conn)
        new_status = engine.accept_intake(
            application_id,
            actor,
            confirmed_additional_disciplines=body.confirmed_additional_disciplines,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rt.discipline_code, rt.status, rt.sla_due_at, r.full_name AS reviewer
                FROM review_task rt
                LEFT JOIN reviewer r ON r.id = rt.reviewer_id
                WHERE rt.application_id = %s AND rt.round = 1
                ORDER BY rt.discipline_code
                """,
                (str(application_id),),
            )
            tasks = cur.fetchall()
        return {"status": new_status.value, "review_tasks": tasks}


@router.post("/{application_id}/resubmit")
def resubmit(application_id: UUID, actor: CurrentActor) -> dict:
    """Applicant responded. Resumes the clock and reopens deficient disciplines only."""
    with transaction() as conn:
        engine = Engine(conn)
        new_status = engine.resubmit(application_id, actor)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT discipline_code, round, status FROM review_task
                WHERE application_id = %s ORDER BY discipline_code, round
                """,
                (str(application_id),),
            )
            return {"status": new_status.value, "review_tasks": cur.fetchall()}


@router.post("/{application_id}/issue")
def issue(application_id: UUID, actor: CurrentActor) -> dict:
    """Issue the permit (FR-18). Supervisor only, refused while any discipline is open."""
    with transaction() as conn:
        return {"status": Engine(conn).issue(application_id, actor).value}


@router.post("/{application_id}/deny")
def deny(application_id: UUID, body: Deny, actor: CurrentActor) -> dict:
    """Deny the permit (FR-19). The reason is recorded on the case."""
    with transaction() as conn:
        return {"status": Engine(conn).deny(application_id, actor, body.reason).value}


@router.post("/{application_id}/withdraw")
def withdraw(application_id: UUID, body: Withdraw, actor: CurrentActor) -> dict:
    with transaction() as conn:
        return {"status": Engine(conn).withdraw(application_id, actor, body.reason).value}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.put("/{application_id}/documents/{document_type_code}")
def receive_document(
    application_id: UUID,
    document_type_code: str,
    body: DocumentReceived,
    actor: CurrentActor,
) -> dict:
    """Mark a required document as received.

    File storage is out of scope for Phase 1, so this records the filename and the
    uploader rather than accepting bytes. The checklist behaviour it drives is the part
    that matters to FR-04.
    """
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE application_document
                SET status = 'RECEIVED', filename = %s, uploaded_at = now(), uploaded_by = %s
                WHERE application_id = %s AND document_type_code = %s AND status <> 'SUPERSEDED'
                RETURNING id
                """,
                (body.filename, actor.username, str(application_id), document_type_code),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"{document_type_code} is not on this application's checklist")

        audit.record(
            conn,
            entity_type="document",
            entity_id=row["id"],
            action="receive",
            actor=actor.username,
            after={"document_type_code": document_type_code, "filename": body.filename},
        )
        return {
            "document_type_code": document_type_code,
            "outstanding": checklist.missing_document_codes(conn, application_id),
        }


@router.post("/{application_id}/documents/{document_type_code}/waive")
def waive_document(
    application_id: UUID, document_type_code: str, body: WaiveDocument, actor: CurrentActor
) -> dict:
    """Waive a required document. A waiver is a decision, so it carries an owner and a reason."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE application_document
                SET status = 'WAIVED', waived_by = %s, waiver_reason = %s
                WHERE application_id = %s AND document_type_code = %s AND status = 'MISSING'
                RETURNING id
                """,
                (actor.username, body.reason, str(application_id), document_type_code),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"no outstanding {document_type_code} on this application")

        audit.record(
            conn,
            entity_type="document",
            entity_id=row["id"],
            action="waive",
            actor=actor.username,
            after={"document_type_code": document_type_code, "reason": body.reason},
        )
        return {
            "document_type_code": document_type_code,
            "outstanding": checklist.missing_document_codes(conn, application_id),
        }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@router.get("/{application_id}", response_model=ApplicationView)
def get_application(application_id: UUID, actor: CurrentActor) -> ApplicationView:
    """Status, party, and SLA position in one read. Backs US-02."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.*, sla.sla_state, sla.net_business_days_elapsed,
                   sla.allowance_days, sla.days_remaining
            FROM v_application_summary s
            JOIN v_sla_status sla ON sla.application_id = s.application_id
            WHERE s.application_id = %s
            """,
            (str(application_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"application {application_id} not found")
    return ApplicationView(**row)


@router.get("/{application_id}/history")
def get_history(application_id: UUID, actor: CurrentActor) -> dict:
    """Full ordered history of the case (US-16)."""
    with read_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT from_status, to_status, actor, reason, occurred_at
                FROM status_history WHERE application_id = %s ORDER BY occurred_at, id
                """,
                (str(application_id),),
            )
            transitions = cur.fetchall()
        return {
            "transitions": transitions,
            "audit": audit.history(conn, "application", application_id),
        }
