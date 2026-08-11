"""Screens for department staff.

Every query here reads a view that already exists. The screens are a rendering of the
reporting layer, so a number on a page and the same number from the JSON API come from one
piece of SQL and cannot drift.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import audit
from ..ai import rag, recording
from ..ai.retrieval import load_index
from ..config import get_settings
from ..db import read_connection, transaction
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


# ---------------------------------------------------------------------------
# Triage review
# ---------------------------------------------------------------------------

def _pending_extraction(conn, application_id: UUID) -> dict | None:
    """The extraction awaiting a decision, newest first if triage was run twice."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, payload, model, prompt_version, confidence, created_at
               FROM ai_recommendation
               WHERE application_id = %s AND kind = 'field_extraction' AND accepted IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (str(application_id),),
        )
        return cur.fetchone()


def _decided_extractions(conn, application_id: UUID) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT payload, override_payload, accepted, decided_by, decided_at
               FROM ai_recommendation
               WHERE application_id = %s AND kind = 'field_extraction'
                 AND accepted IS NOT NULL
               ORDER BY decided_at DESC""",
            (str(application_id),),
        )
        return cur.fetchall()


def _routing_recommendation(conn, application_id: UUID) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT payload, model, prompt_version
               FROM ai_recommendation
               WHERE application_id = %s AND kind = 'discipline_routing'
               ORDER BY created_at DESC LIMIT 1""",
            (str(application_id),),
        )
        return cur.fetchone()


@router.get("/case/{application_id}/triage", response_class=HTMLResponse)
def triage_review(application_id: UUID, request: Request):
    """What the model drafted, what it declined to draft, and the evidence for both."""
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    with read_connection() as conn:
        case = _summary(conn, application_id)
        if case is None:
            raise HTTPException(status_code=404, detail="no such application")
        # The summary view carries no narrative, and this is the one screen where the
        # applicant's own words have to sit next to what was drafted from them.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_narrative FROM application WHERE id = %s", (str(application_id),)
            )
            case["scope_narrative"] = cur.fetchone()["scope_narrative"]

        pending = _pending_extraction(conn, application_id)
        decided = _decided_extractions(conn, application_id)
        # The fields panels read from whichever extraction is most relevant. After a
        # decision the draft is still the interesting part of the page, and hiding it would
        # leave a clerk who just accepted looking at an empty screen.
        latest = pending or (decided[0] if decided else None)
        context = {
            "actor": actor,
            "case": case,
            "pending": pending,
            "latest": latest,
            "decided": decided,
            "routing": _routing_recommendation(conn, application_id),
            "threshold": get_settings().ai_field_confidence_threshold,
            "may_decide": actor.role in (Role.INTAKE_CLERK, Role.SUPERVISOR),
        }

    return TEMPLATES.TemplateResponse(request=request, name="triage.html", context=context)


@router.post("/case/{application_id}/triage")
def decide_triage(
    application_id: UUID,
    request: Request,
    recommendation_id: UUID = Form(...),
    action: str = Form(...),
    reason: str = Form(""),
):
    """Accept the draft as it stands, or override it with a reason.

    Only clerks and supervisors decide. The check is here and in `recording.decide`, which
    refuses a second decision on the same row: the first decision is the one that moved the
    case, and letting it be overwritten would lose the actor who made it.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    if actor.role not in (Role.INTAKE_CLERK, Role.SUPERVISOR):
        raise HTTPException(status_code=403, detail=f"role {actor.role} may not decide triage")

    if action == "override" and not reason.strip():
        # An override with no reason is the audit row that explains nothing two years later.
        raise HTTPException(status_code=422, detail="an override needs a reason")

    with transaction() as conn:
        recording.decide(
            conn,
            recommendation_id,
            actor=actor.username,
            accepted=(action == "accept"),
            override_payload={"reason": reason.strip()} if action == "override" else None,
        )

    return RedirectResponse(url=f"/ui/case/{application_id}/triage", status_code=303)


# ---------------------------------------------------------------------------
# Ordinance questions
# ---------------------------------------------------------------------------

#: Offered on the empty screen. The last one is there deliberately: the ordinance says
#: nothing about helipads, and the answer a reviewer should get is that it says nothing.
EXAMPLE_QUESTIONS = [
    "What is the rear setback in R-90?",
    "When is a stormwater management plan required?",
    "What is the maximum building height in C-2?",
    "How many parking spaces does a helipad require?",
]


@router.get("/ordinance", response_class=HTMLResponse)
def ordinance(request: Request, q: str = "", case: str = ""):
    """Ask the zoning ordinance a question and get its own words back.

    The model never writes the quote. Retrieval returns a section number, code reads the
    text back out of the corpus by that number, and `rag.verify` asserts the quote appears
    byte for byte in the source before any of it reaches this page. A verification failure
    withholds the answer here, and does not show it with a warning attached, because a
    quote that cannot be found in the ordinance is not a quote.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    answer = None
    sections: dict[str, str] = {}
    verification_error = None
    question = q.strip()

    if question:
        index = load_index()
        try:
            # answer_question verifies before it returns, so the failure surfaces here.
            # Catching it at the screen turns an assertion into a withheld answer, and not
            # a 500 in front of a reviewer.
            answer = rag.answer_question(question, index=index)
        except AssertionError as exc:
            verification_error = str(exc)
            answer = None
        else:
            # The full provision each quote was taken from, so a reviewer can read the
            # quote in its context and not take the system's word for the boundaries.
            for citation in answer.citations:
                section = index.by_id(citation.section_id)
                if section is not None:
                    sections[citation.section_id] = section.text

    return TEMPLATES.TemplateResponse(
        request=request,
        name="ordinance.html",
        context={
            "actor": actor,
            "question": question,
            "answer": answer,
            "sections": sections,
            "verification_error": verification_error,
            "examples": EXAMPLE_QUESTIONS,
            "case_id": case,
            "corpus_size": len(load_index()),
        },
    )


# ---------------------------------------------------------------------------
# Supervisor dashboard
# ---------------------------------------------------------------------------

#: Quick ranges offered above the dashboard. Months back from today, or None for all time.
DASHBOARD_PRESETS = [
    ("3m", "Last 3 months", 3),
    ("6m", "Last 6 months", 6),
    ("12m", "Last 12 months", 12),
    ("all", "All time", None),
]

DEFAULT_PRESET = "12m"


def _resolve_range(preset: str, start: str, end: str) -> tuple[date | None, date | None, str]:
    """Turn the query string into two bounds and a label a person can read.

    Explicit dates win over a preset, so a link someone pasted keeps meaning what it meant.
    An unparseable date falls back to the default range: a dashboard that 500s on a typo in
    the address bar is worse than one that quietly shows the usual year.
    """
    parsed_start = _as_date(start)
    parsed_end = _as_date(end)
    if parsed_start or parsed_end:
        label = f"{parsed_start or 'the beginning'} to {parsed_end or 'today'}"
        return parsed_start, parsed_end, label

    chosen = preset if preset in {key for key, _, _ in DASHBOARD_PRESETS} else DEFAULT_PRESET
    for key, text, months in DASHBOARD_PRESETS:
        if key == chosen:
            if months is None:
                return None, None, text.lower()
            # Counted in calendar months, not in 31 day chunks, which is what a department
            # means by "the last three months" and also what stops the window being four.
            # Anchored to the first of the month so a partial month at the edge does not
            # drag the rate down with cases that have not been decided yet.
            today = date.today()
            index = today.year * 12 + (today.month - 1) - (months - 1)
            first = date(index // 12, index % 12 + 1, 1)
            return first, None, text.lower()
    return None, None, "all time"


def _as_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _headline(conn, start: date | None, end: date | None) -> dict:
    """The four numbers a department director is accountable for.

    Read from the same views the per-month tables read, with the same bounds, so the
    summary at the top of the page and the rows below it cannot disagree. Computing the
    headline separately in Python is how a dashboard ends up contradicting itself.

    Open cases deliberately ignore the range. "How many are open" is a question about now,
    and filtering it by a decided-date window produces a number nobody can interpret.
    """
    bounds = {"start": start, "end": end}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sum(decided_count)                                   AS decided,
                   sum(met_count)                                       AS met,
                   round(100.0 * sum(met_count) / nullif(sum(decided_count), 0), 1)
                                                                        AS compliance_pct
            FROM v_sla_compliance
            WHERE (%(start)s::date IS NULL OR decided_month >= date_trunc('month', %(start)s::date))
              AND (%(end)s::date   IS NULL OR decided_month <= date_trunc('month', %(end)s::date))
            """,
            bounds,
        )
        compliance = cur.fetchone()

        cur.execute(
            """
            SELECT round(avg(gross_business_days), 1)          AS mean_gross,
                   round(avg(net_business_days), 1)            AS mean_net,
                   round(avg(applicant_wait_business_days), 1) AS mean_wait,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY net_business_days) AS median_net,
                   round(percentile_cont(0.9) WITHIN GROUP (ORDER BY net_business_days)::numeric, 1)
                                                               AS p90_net,
                   count(*)                                    AS decided
            FROM v_cycle_time
            WHERE decided_at IS NOT NULL
              AND (%(start)s::date IS NULL OR decided_at >= %(start)s::date)
              AND (%(end)s::date   IS NULL OR decided_at < %(end)s::date + 1)
            """,
            bounds,
        )
        cycle = cur.fetchone()

        cur.execute(
            """
            SELECT count(*) FILTER (WHERE status NOT IN
                        ('ISSUED','DENIED','WITHDRAWN','EXPIRED')) AS open_cases,
                   count(*) FILTER (WHERE clock_paused)            AS paused_cases
            FROM v_application_summary
            """
        )
        volume = cur.fetchone()

    return {"compliance": compliance, "cycle": cycle, "volume": volume}


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    preset: str = DEFAULT_PRESET,
    start: str = "",
    end: str = "",
):
    """Where the department stands: compliance, cycle time, workload, and what is stuck.

    Every panel is a thin read over a view. The arithmetic stays in SQL because the
    compliance figure goes to the city council, and a number recomputed in application
    code is a number that will eventually disagree with the report it came from.

    The range applies to decided cases only. Workload, disciplines, and escalations are
    current state, and are labelled as such on the page. A period filter silently applied
    to "what is open right now" produces a number that reads as an answer and is not one.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    from_date, to_date, range_label = _resolve_range(preset, start, end)
    bounds = {"start": from_date, "end": to_date}

    with read_connection() as conn:
        headline = _headline(conn, from_date, to_date)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT decided_month, permit_type_code, permit_type_name,
                       council_standard_days, decided_count, met_count, compliance_pct,
                       mean_net_days, median_net_days, p90_net_days
                FROM v_sla_compliance
                WHERE (%(start)s::date IS NULL OR decided_month >= date_trunc('month', %(start)s::date))
                  AND (%(end)s::date   IS NULL OR decided_month <= date_trunc('month', %(end)s::date))
                ORDER BY decided_month DESC, permit_type_code
                """,
                bounds,
            )
            compliance_rows = cur.fetchall()

            cur.execute(
                """
                SELECT decided_month,
                       sum(decided_count)                                        AS decided,
                       sum(met_count)                                            AS met,
                       round(100.0 * sum(met_count) / nullif(sum(decided_count), 0), 1)
                                                                                 AS compliance_pct
                FROM v_sla_compliance
                WHERE (%(start)s::date IS NULL OR decided_month >= date_trunc('month', %(start)s::date))
                  AND (%(end)s::date   IS NULL OR decided_month <= date_trunc('month', %(end)s::date))
                GROUP BY decided_month
                ORDER BY decided_month
                """,
                bounds,
            )
            monthly = cur.fetchall()

            cur.execute("SELECT * FROM v_discipline_bottleneck ORDER BY open_tasks DESC, discipline_code")
            disciplines = cur.fetchall()

            cur.execute(
                """
                SELECT username, full_name, discipline_code, active, open_tasks,
                       completed_last_30_days, oldest_open_business_days
                FROM v_reviewer_workload
                WHERE open_tasks > 0 OR completed_last_30_days > 0
                ORDER BY open_tasks DESC, oldest_open_business_days DESC NULLS LAST
                """
            )
            workload = cur.fetchall()

            cur.execute("SELECT * FROM v_open_escalations")
            escalations = cur.fetchall()

    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "actor": actor,
            "headline": headline,
            "compliance_rows": compliance_rows,
            "monthly": monthly,
            "disciplines": disciplines,
            "workload": workload,
            "escalations": escalations,
            "presets": DASHBOARD_PRESETS,
            "active_preset": preset if not (from_date and start) else "",
            "range_label": range_label,
            "range_start": start,
            "range_end": end,
        },
    )


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

#: Codes are used in URLs, SQL filters, and the routing tables, so they are constrained to
#: something that cannot surprise any of those.
DISCIPLINE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,29}$")


def require_supervisor(actor) -> None:
    if actor.role != Role.SUPERVISOR:
        raise HTTPException(
            status_code=403, detail=f"role {actor.role} may not change configuration"
        )


def _discipline_rows(conn) -> list[dict]:
    """Every discipline with what it is wired into, so the page shows readiness.

    A discipline that exists but is routed to nothing, or has nobody certified, is
    configured and useless. The counts are here so that state is visible on the row
    instead of being discovered when a case sits unassigned.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.code, d.name, d.description, d.active,
                   (SELECT count(*) FROM permit_type_discipline p
                     WHERE p.discipline_code = d.code AND p.always_required)  AS always_on,
                   (SELECT count(*) FROM permit_type_discipline p
                     WHERE p.discipline_code = d.code AND NOT p.always_required) AS candidate_on,
                   (SELECT count(*) FROM reviewer_discipline rd
                     JOIN reviewer r ON r.id = rd.reviewer_id
                     WHERE rd.discipline_code = d.code AND r.active)          AS certified_reviewers,
                   (SELECT count(*) FROM review_task t
                     WHERE t.discipline_code = d.code
                       AND t.status IN ('PENDING','ASSIGNED','IN_PROGRESS'))  AS open_tasks
            FROM discipline d
            ORDER BY d.active DESC, d.code
            """
        )
        return cur.fetchall()


def _permit_types(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, council_standard_days FROM permit_type ORDER BY category, code"
        )
        return cur.fetchall()


def _reviewers(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, full_name FROM reviewer WHERE active AND role = 'reviewer'"
            " ORDER BY full_name"
        )
        return cur.fetchall()


@router.get("/admin", response_class=HTMLResponse)
def admin(
    request: Request,
    saved: str = "",
    error: str = "",
    confirm_type: str = "",
    confirm_days: int = 0,
):
    """Configuration a department can change without a developer.

    Everything on this screen is a row in a reference table. Nothing here is a migration,
    a deploy, or a code change, which is the answer to what happens when the consultant
    leaves.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)

    with read_connection() as conn:
        pending_standard = None
        if confirm_type and confirm_days:
            # A retroactive change waits for a second click, and the page shows the size of
            # the rewrite it would cause before anybody makes it.
            pending_standard = {
                "permit_type_code": confirm_type,
                "current": _current_standard(conn, confirm_type),
                "proposed": confirm_days,
                "impact": _standard_impact(conn, confirm_type, confirm_days),
            }

        context = {
            "actor": actor,
            "may_edit": actor.role == Role.SUPERVISOR,
            "disciplines": _discipline_rows(conn),
            "permit_types": _permit_types(conn),
            "reviewers": _reviewers(conn),
            "policies": _phase_allowances(conn),
            "pending_standard": pending_standard,
            "saved": saved,
            "error": error,
        }

    return TEMPLATES.TemplateResponse(request=request, name="admin.html", context=context)


@router.post("/admin/disciplines")
def add_discipline(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    always_required: list[str] = Form(default=[]),
    candidate: list[str] = Form(default=[]),
    reviewers: list[str] = Form(default=[]),
):
    """Add a review discipline and wire it into routing and certification.

    Four tables, and the screen does all four in one transaction, because a discipline
    that exists in one of them and not the others is worse than one that does not exist:
    it routes work nobody is certified to do, or it is certified and never routed.

    The SLA allowance is deliberately not one of them. `sla_policy` falls back to the row
    with a null discipline, so a new discipline inherits its permit type's review
    allowance until somebody decides it needs its own.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)
    require_supervisor(actor)

    code = code.strip().upper()
    name = name.strip()

    if not DISCIPLINE_CODE.match(code):
        return RedirectResponse(
            url="/ui/admin?error=" + quote(
                f"{code or 'the code'} is not a usable code. Three to thirty characters, "
                "starting with a letter, upper case letters, digits, and underscores."
            ),
            status_code=303,
        )
    if not name:
        return RedirectResponse(url="/ui/admin?error=" + quote("A name is required."), status_code=303)

    # A permit type cannot be both standing and candidate. Standing wins, since it is the
    # stronger statement and silently dropping one of the two would be worse.
    candidate = [c for c in candidate if c not in always_required]

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM discipline WHERE code = %s", (code,))
            if cur.fetchone():
                return RedirectResponse(
                    url="/ui/admin?error=" + quote(f"{code} already exists."), status_code=303
                )

            cur.execute(
                "INSERT INTO discipline (code, name, description) VALUES (%s, %s, %s)",
                (code, name, description.strip() or None),
            )

            for permit_type in always_required:
                cur.execute(
                    """INSERT INTO permit_type_discipline (permit_type_code, discipline_code, always_required)
                       VALUES (%s, %s, true)""",
                    (permit_type, code),
                )
            for permit_type in candidate:
                cur.execute(
                    """INSERT INTO permit_type_discipline (permit_type_code, discipline_code, always_required)
                       VALUES (%s, %s, false)""",
                    (permit_type, code),
                )
            for reviewer_id in reviewers:
                cur.execute(
                    """INSERT INTO reviewer_discipline (reviewer_id, discipline_code, certified_on)
                       VALUES (%s, %s, current_date)""",
                    (reviewer_id, code),
                )

        audit.record(
            conn,
            entity_type="configuration",
            entity_id=None,
            action="add:discipline",
            actor=actor.username,
            after={
                "code": code,
                "name": name,
                "always_required": sorted(always_required),
                "candidate": sorted(candidate),
                "certified_reviewers": len(reviewers),
            },
        )

    return RedirectResponse(url="/ui/admin?saved=" + quote(code), status_code=303)


def _phase_allowances(conn) -> list[dict]:
    """Every SLA policy row, with the council standard it has to add up to.

    The phases sum to the standard on purpose: 3 plus 14 plus 3 is the 20 day residential
    commitment. Showing the sum next to the standard is what makes an edit that breaks the
    arithmetic visible at the moment it is made.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.permit_type_code, pt.name AS permit_type_name,
                   pt.council_standard_days, p.phase, p.discipline_code,
                   p.allowance_days, p.warning_threshold,
                   sum(p.allowance_days) FILTER (WHERE p.discipline_code IS NULL)
                       OVER (PARTITION BY p.permit_type_code) AS base_phase_total
            FROM sla_policy p
            JOIN permit_type pt ON pt.code = p.permit_type_code
            ORDER BY pt.category, p.permit_type_code,
                     CASE p.phase WHEN 'INTAKE_SCREENING' THEN 0
                                  WHEN 'REVIEW_TASK' THEN 1 ELSE 2 END,
                     p.discipline_code NULLS FIRST
            """
        )
        return cur.fetchall()


def _standard_impact(conn, permit_type_code: str, proposed: int) -> dict:
    """What moving the council standard would do to already-published compliance.

    `v_sla_compliance` joins `permit_type` live, so the standard is applied to cases that
    were decided years ago. Raising it turns past misses into hits with no record that the
    number moved. The screen cannot make the view stop doing that without denormalising the
    standard onto every application, so it does the next best thing and shows the size of
    the rewrite before anybody agrees to it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)                                                    AS decided,
                   count(*) FILTER (WHERE net_business_days <= %(current)s)    AS met_now,
                   count(*) FILTER (WHERE net_business_days <= %(proposed)s)   AS met_after
            FROM v_cycle_time
            WHERE permit_type_code = %(code)s AND decided_at IS NOT NULL
            """,
            {"code": permit_type_code, "current": _current_standard(conn, permit_type_code),
             "proposed": proposed},
        )
        row = cur.fetchone()

    decided = row["decided"] or 0
    return {
        "decided": decided,
        "met_now": row["met_now"] or 0,
        "met_after": row["met_after"] or 0,
        "moved": (row["met_after"] or 0) - (row["met_now"] or 0),
        "pct_now": round(100.0 * (row["met_now"] or 0) / decided, 1) if decided else None,
        "pct_after": round(100.0 * (row["met_after"] or 0) / decided, 1) if decided else None,
    }


def _current_standard(conn, permit_type_code: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT council_standard_days FROM permit_type WHERE code = %s", (permit_type_code,))
        row = cur.fetchone()
    return row["council_standard_days"] if row else 0


@router.post("/admin/sla")
def change_allowance(
    request: Request,
    policy_id: UUID = Form(...),
    allowance_days: int = Form(...),
):
    """Change a phase allowance.

    Open tasks keep the allowance they opened with. `review_task.allowance_days` is written
    when the task is created and the SLA view reads it from the row, so a supervisor cannot
    make a reviewer late, or on time, by editing configuration underneath them. New tasks
    take the new number.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)
    require_supervisor(actor)

    if not 1 <= allowance_days <= 365:
        return RedirectResponse(
            url="/ui/admin?error=" + quote("An allowance has to be between 1 and 365 days."),
            status_code=303,
        )

    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.permit_type_code, p.phase, p.discipline_code, p.allowance_days
                   FROM sla_policy p WHERE p.id = %s""",
                (str(policy_id),),
            )
            before = cur.fetchone()
            if before is None:
                raise HTTPException(status_code=404, detail="no such policy")

            cur.execute(
                "UPDATE sla_policy SET allowance_days = %s WHERE id = %s",
                (allowance_days, str(policy_id)),
            )

            cur.execute(
                """SELECT count(*) AS n FROM review_task
                   WHERE status IN ('PENDING','ASSIGNED','IN_PROGRESS')
                     AND application_id IN (SELECT id FROM application WHERE permit_type_code = %s)""",
                (before["permit_type_code"],),
            )
            unaffected = cur.fetchone()["n"]

        audit.record(
            conn,
            entity_type="configuration",
            entity_id=None,
            action="change:sla_allowance",
            actor=actor.username,
            before={
                "permit_type_code": before["permit_type_code"],
                "phase": before["phase"],
                "discipline_code": before["discipline_code"],
                "allowance_days": before["allowance_days"],
            },
            after={"allowance_days": allowance_days, "open_tasks_left_alone": unaffected},
        )

    return RedirectResponse(
        url="/ui/admin?saved=" + quote(
            f"{before['permit_type_code']} {before['phase']} allowance is now {allowance_days} days. "
            f"{unaffected} open task(s) keep the allowance they opened with."
        ),
        status_code=303,
    )


@router.post("/admin/council-standard")
def change_council_standard(
    request: Request,
    permit_type_code: str = Form(...),
    council_standard_days: int = Form(...),
    confirm: str = Form(""),
):
    """Change the council standard a permit type is measured against.

    Unlike a phase allowance, this is retroactive. The compliance view applies the current
    standard to every case ever decided, so moving it rewrites what the department has
    already reported. The change is allowed, because the standard genuinely does change when
    a council votes, but it needs an explicit confirmation and the audit row carries the
    compliance figure before and after so the movement is attributable later.
    """
    try:
        actor = current_actor(request)
    except NotSignedIn:
        return to_picker(request)
    require_supervisor(actor)

    if not 1 <= council_standard_days <= 365:
        return RedirectResponse(
            url="/ui/admin?error=" + quote("A council standard has to be between 1 and 365 days."),
            status_code=303,
        )

    with transaction() as conn:
        current = _current_standard(conn, permit_type_code)
        if current == council_standard_days:
            return RedirectResponse(
                url="/ui/admin?error=" + quote(f"{permit_type_code} is already {current} days."),
                status_code=303,
            )

        impact = _standard_impact(conn, permit_type_code, council_standard_days)

        if confirm != "yes" and impact["moved"]:
            return RedirectResponse(
                url=(
                    f"/ui/admin?confirm_type={quote(permit_type_code)}"
                    f"&confirm_days={council_standard_days}"
                ),
                status_code=303,
            )

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE permit_type SET council_standard_days = %s WHERE code = %s",
                (council_standard_days, permit_type_code),
            )

        audit.record(
            conn,
            entity_type="configuration",
            entity_id=None,
            action="change:council_standard",
            actor=actor.username,
            before={
                "permit_type_code": permit_type_code,
                "council_standard_days": current,
                "reported_compliance_pct": impact["pct_now"],
            },
            after={
                "council_standard_days": council_standard_days,
                "reported_compliance_pct": impact["pct_after"],
                "decided_cases_reclassified": impact["moved"],
            },
        )

    return RedirectResponse(
        url="/ui/admin?saved=" + quote(
            f"{permit_type_code} standard is now {council_standard_days} days. "
            f"{abs(impact['moved'])} already-decided case(s) changed side, and reported "
            f"compliance moved from {impact['pct_now']}% to {impact['pct_after']}%."
        ),
        status_code=303,
    )
