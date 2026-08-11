"""Screens for department staff.

Every query here reads a view that already exists. The screens are a rendering of the
reporting layer, so a number on a page and the same number from the JSON API come from one
piece of SQL and cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db import read_connection
from ..process.states import Role
from .deps import ACTOR_COOKIE, NotSignedIn, current_actor, staff_directory, to_picker

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

#: How a task's SLA position is shown. The view already decides the state; this is only
#: the label and the css class, so the screen cannot invent a fifth state of its own.
SLA_LABELS = {
    "BREACHED": ("Overdue", "sla-breached"),
    "AT_RISK": ("Due soon", "sla-at-risk"),
    "ON_TRACK": ("On track", "sla-on-track"),
    "UNASSIGNED": ("Unassigned", "sla-unassigned"),
    "CLOSED": ("Closed", "sla-closed"),
}


@router.get("/", response_class=HTMLResponse)
def picker(request: Request, next: str = "/ui/queue") -> HTMLResponse:
    """Choose who to act as. Stands in for the city SSO that would sit here."""
    return TEMPLATES.TemplateResponse(
        request=request,
        name="picker.html",
        context={"staff": staff_directory(), "next": next, "actor": None},
    )


@router.post("/sign-in")
def sign_in(username: str = Form(...), next: str = Form("/ui/queue")) -> RedirectResponse:
    response = RedirectResponse(url=next, status_code=303)
    # httponly so a script on the page cannot read it. Not a security claim on its own,
    # since the value is a username and not a secret.
    response.set_cookie(ACTOR_COOKIE, username, httponly=True, samesite="lax")
    return response


@router.post("/sign-out")
def sign_out() -> RedirectResponse:
    response = RedirectResponse(url="/ui/", status_code=303)
    response.delete_cookie(ACTOR_COOKIE)
    return response


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    """A reviewer's open work, most at risk first.

    Same query as `GET /queues/mine`. Ordering puts breached first and then by due date,
    because a queue sorted by arrival asks the reviewer to work out what is urgent, which
    is the job the department wanted taken off them.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    if actor.role != Role.REVIEWER:
        # Supervisors and clerks have their own screens. Saying so beats an empty table
        # that looks like the query is broken.
        return TEMPLATES.TemplateResponse(
            request=request,
            name="queue.html",
            context={"actor": actor, "tasks": [], "wrong_role": True, "labels": SLA_LABELS},
        )

    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.review_task_id, t.application_id, t.application_number,
                   t.discipline_code, t.round, t.status, t.business_days_open,
                   t.allowance_days, t.sla_state, t.sla_due_at,
                   s.situs_address, s.permit_type_name, s.applicant_name
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
        tasks = cur.fetchall()

    return TEMPLATES.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "actor": actor,
            "tasks": tasks,
            "wrong_role": False,
            "labels": SLA_LABELS,
            "overdue_count": sum(1 for t in tasks if t["sla_state"] == "BREACHED"),
        },
    )


# ---------------------------------------------------------------------------
# Case detail
# ---------------------------------------------------------------------------

def _summary(conn, application_id: UUID) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM v_application_summary WHERE application_id = %s",
            (str(application_id),),
        )
        return cur.fetchone()


def _sla(conn, application_id: UUID) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM v_sla_status WHERE application_id = %s", (str(application_id),))
        return cur.fetchone()


def _documents(conn, application_id: UUID) -> list[dict]:
    """Required documents with their receipt state. Missing ones first, since those block."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ad.document_type_code, dt.name AS document_name, ad.status,
                   ad.filename, ad.uploaded_at, ad.uploaded_by,
                   ad.waived_by, ad.waiver_reason
            FROM application_document ad
            LEFT JOIN document_type dt ON dt.code = ad.document_type_code
            WHERE ad.application_id = %s
            ORDER BY (ad.status = 'MISSING') DESC, ad.document_type_code
            """,
            (str(application_id),),
        )
        return cur.fetchall()


def _review_tasks(conn, application_id: UUID) -> list[dict]:
    """Every review task including closed earlier rounds.

    Round 1 stays on the page after a resubmission opens round 2. Reading what the
    structural reviewer said the first time is most of what a case history is for.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.review_task_id, t.discipline_code, t.round, t.status,
                   t.reviewer_name, t.reviewer_username, t.assigned_at, t.sla_due_at,
                   t.allowance_days, t.business_days_open, t.sla_state,
                   rt.completed_at
            FROM v_task_sla_status t
            JOIN review_task rt ON rt.id = t.review_task_id
            WHERE t.application_id = %s
            ORDER BY t.round DESC, t.discipline_code
            """,
            (str(application_id),),
        )
        tasks = cur.fetchall()

        cur.execute(
            """
            SELECT d.review_task_id, d.code_reference, d.description, d.severity,
                   d.resolved_at, d.resolved_in_round, d.created_by
            FROM deficiency d
            JOIN review_task rt ON rt.id = d.review_task_id
            WHERE rt.application_id = %s
            ORDER BY d.severity, d.code_reference
            """,
            (str(application_id),),
        )
        deficiencies = cur.fetchall()

        cur.execute(
            """
            SELECT c.review_task_id, c.description, c.created_by
            FROM review_condition c
            JOIN review_task rt ON rt.id = c.review_task_id
            WHERE rt.application_id = %s
            ORDER BY c.created_at
            """,
            (str(application_id),),
        )
        conditions = cur.fetchall()

    for task in tasks:
        task["deficiencies"] = [d for d in deficiencies if d["review_task_id"] == task["review_task_id"]]
        task["conditions"] = [c for c in conditions if c["review_task_id"] == task["review_task_id"]]
    return tasks


def _timeline(conn, application_id: UUID) -> list[dict]:
    """Status changes, clock pauses, and escalations on one axis, newest first.

    Three tables and not one, because a pause is not a status change and folding it
    into one would mean writing a status row that never happened. They are merged here for
    reading and nowhere else.
    """
    events: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT from_status, to_status, actor, reason, occurred_at
               FROM status_history WHERE application_id = %s ORDER BY occurred_at""",
            (str(application_id),),
        )
        for row in cur.fetchall():
            events.append({
                "at": row["occurred_at"],
                "kind": "status",
                "actor": row["actor"],
                "headline": (
                    f"{row['from_status']} to {row['to_status']}" if row["from_status"]
                    else f"created as {row['to_status']}"
                ),
                "detail": row["reason"],
            })

        cur.execute(
            """SELECT paused_at, resumed_at, reason FROM clock_pause
               WHERE application_id = %s ORDER BY paused_at""",
            (str(application_id),),
        )
        for row in cur.fetchall():
            events.append({
                "at": row["paused_at"], "kind": "pause", "actor": "system",
                "headline": "SLA clock paused",
                "detail": f"waiting on the applicant ({row['reason']})",
            })
            if row["resumed_at"]:
                events.append({
                    "at": row["resumed_at"], "kind": "pause", "actor": "system",
                    "headline": "SLA clock resumed", "detail": None,
                })

        cur.execute(
            """SELECT e.level, e.reason, e.raised_at, e.acknowledged_at, e.acknowledged_by
               FROM escalation e WHERE e.application_id = %s ORDER BY e.raised_at""",
            (str(application_id),),
        )
        for row in cur.fetchall():
            events.append({
                "at": row["raised_at"], "kind": "escalation", "actor": "system",
                "headline": f"escalation raised ({row['level']})", "detail": row["reason"],
            })
            if row["acknowledged_at"]:
                events.append({
                    "at": row["acknowledged_at"], "kind": "escalation",
                    "actor": row["acknowledged_by"], "headline": "escalation acknowledged",
                    "detail": None,
                })

    return sorted(events, key=lambda e: e["at"], reverse=True)


def _audit(conn, application_id: UUID) -> list[dict]:
    """Raw audit rows for the case and everything hanging off it.

    Tasks, escalations, and AI recommendations are separate entities in the log, so a case
    view has to gather their ids. Reading only entity_type='application' would show the
    status changes and hide who approved what.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT al.entity_type, al.entity_id, al.action, al.actor,
                   al.before_value, al.after_value, al.occurred_at
            FROM audit_log al
            WHERE al.entity_id = %(app)s
               OR al.entity_id IN (SELECT id FROM review_task WHERE application_id = %(app)s)
               OR al.entity_id IN (SELECT id FROM escalation WHERE application_id = %(app)s)
               OR al.entity_id IN (SELECT id FROM ai_recommendation WHERE application_id = %(app)s)
            ORDER BY al.occurred_at DESC, al.id DESC
            """,
            {"app": str(application_id)},
        )
        return cur.fetchall()


def _recommendations(conn, application_id: UUID) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT kind, model, prompt_version, confidence, accepted, decided_by,
                      decided_at, created_at
               FROM ai_recommendation WHERE application_id = %s
               ORDER BY created_at""",
            (str(application_id),),
        )
        return cur.fetchall()


def _integrations(conn, application_id: UUID) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT system, operation, status, attempt, error_detail, latency_ms,
                      attempted_at
               FROM integration_call WHERE application_id = %s
               ORDER BY attempted_at DESC, attempt DESC""",
            (str(application_id),),
        )
        return cur.fetchall()


@router.get("/case/{application_id}", response_class=HTMLResponse)
def case_detail(application_id: UUID, request: Request):
    """Everything on one case: parties, documents, reviews, clock, timeline, audit."""
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    with read_connection() as conn:
        summary = _summary(conn, application_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="no such application")

        context = {
            "actor": actor,
            "case": summary,
            "sla": _sla(conn, application_id),
            "documents": _documents(conn, application_id),
            "tasks": _review_tasks(conn, application_id),
            "timeline": _timeline(conn, application_id),
            "audit": _audit(conn, application_id),
            "recommendations": _recommendations(conn, application_id),
            "integrations": _integrations(conn, application_id),
            "labels": SLA_LABELS,
        }

    return TEMPLATES.TemplateResponse(request=request, name="case.html", context=context)
