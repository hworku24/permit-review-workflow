"""Screens for department staff.

Every query here reads a view that already exists. The screens are a rendering of the
reporting layer, so a number on a page and the same number from the JSON API come from one
piece of SQL and cannot drift.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
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
