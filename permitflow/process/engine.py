"""The case lifecycle engine.

Everything that moves an application or a review task goes through here. The engine owns
three guarantees:

1. No transition happens that `states.py` does not permit, and no transition happens that
   the actor's role does not permit.
2. `application.status`, the newest `status_history` row, and the audit log are written in
   one transaction, so the denormalized status can never disagree with the history.
3. The SLA clock pauses and resumes as a consequence of the target state rather than as
   something each action remembers to do. An action that forgets is the bug that produces
   a wrong compliance number, so no action is trusted to remember.

Callers supply the connection and own the transaction. See `permitflow.db.transaction`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from .. import audit
from ..db import load_holiday_calendar, next_application_number
from ..errors import GuardFailed, InvalidTransition, NotFound, PermissionDenied
from . import assignment, checklist, routing
from .sla import HolidayCalendar, add_business_days
from .states import (
    APPROVING_TASK_STATUSES,
    CLOCK_PAUSED_STATES,
    OPEN_TASK_STATUSES,
    TERMINAL_STATES,
    TERMINAL_TASK_STATUSES,
    Action,
    ApplicationStatus,
    Role,
    TaskAction,
    TaskStatus,
    next_status,
    next_task_status,
    role_may,
    role_may_task,
)


@dataclass(frozen=True)
class Actor:
    """Who is performing an action.

    Carried explicitly rather than pulled from a thread local so that every audit row has
    a real name attached and so the engine can be driven from a script, a test, or the API
    without three different ways of answering "who did this".
    """

    username: str
    role: Role
    reviewer_id: UUID | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(username="system", role=Role.SYSTEM)


def _now() -> datetime:
    return datetime.now(UTC)


class Engine:
    """Drives the case lifecycle.

    `clock` is injectable so SLA behaviour can be tested without waiting fourteen real
    business days, and so `scripts/seed_cases.py` can build a year of history that is
    internally consistent instead of backdating timestamps after the fact. Everything in
    this class reads the time through `self._now()` and never through `datetime.now`.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        calendar: HolidayCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.calendar = calendar if calendar is not None else load_holiday_calendar(conn)
        self._clock = clock or _now

    def _now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_application(self, application_id: UUID, *, for_update: bool = False) -> dict[str, Any]:
        sql = "SELECT * FROM application WHERE id = %s"
        if for_update:
            sql += " FOR UPDATE"
        with self.conn.cursor() as cur:
            cur.execute(sql, (str(application_id),))
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"application {application_id}")
        return row

    def get_task(self, task_id: UUID, *, for_update: bool = False) -> dict[str, Any]:
        sql = "SELECT * FROM review_task WHERE id = %s"
        if for_update:
            sql += " FOR UPDATE"
        with self.conn.cursor() as cur:
            cur.execute(sql, (str(task_id),))
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"review_task {task_id}")
        return row

    def _allowance(self, permit_type_code: str, phase: str, discipline_code: str | None = None) -> tuple[int, float]:
        """SLA allowance for a phase, preferring a discipline-specific policy."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT allowance_days, warning_threshold
                FROM sla_policy
                WHERE permit_type_code = %s
                  AND phase = %s
                  AND (discipline_code = %s OR discipline_code IS NULL)
                ORDER BY discipline_code NULLS LAST
                LIMIT 1
                """,
                (permit_type_code, phase, discipline_code),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound(f"sla_policy for {permit_type_code}/{phase}")
        return row["allowance_days"], float(row["warning_threshold"])

    # ------------------------------------------------------------------
    # Core transition
    # ------------------------------------------------------------------

    def _transition(
        self,
        application: dict[str, Any],
        action: Action,
        actor: Actor,
        *,
        reason: str | None = None,
        extra_columns: dict[str, Any] | None = None,
    ) -> ApplicationStatus:
        current = ApplicationStatus(application["status"])
        target = next_status(current, action)
        if target is None:
            raise InvalidTransition("application", current, action)
        if not role_may(actor.role, action):
            raise PermissionDenied(f"role {actor.role} may not {action}")

        moment = self._now()
        columns = dict(extra_columns or {})
        columns["status"] = target.value
        columns["status_since"] = moment

        assignments = ", ".join(f"{name} = %s" for name in columns)
        params = list(columns.values()) + [str(application["id"])]

        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE application SET {assignments} WHERE id = %s", params)
            cur.execute(
                """
                INSERT INTO status_history (application_id, from_status, to_status, actor, reason, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (str(application["id"]), current.value, target.value, actor.username, reason, moment),
            )

        # The clock follows the state. No action decides this for itself.
        self._sync_clock(application["id"], current, target, moment)

        audit.record(
            self.conn,
            entity_type="application",
            entity_id=application["id"],
            action=f"transition:{action}",
            actor=actor.username,
            before={"status": current.value},
            after={"status": target.value, "reason": reason},
        )

        application["status"] = target.value
        return target

    def _sync_clock(
        self,
        application_id: UUID,
        previous: ApplicationStatus,
        target: ApplicationStatus,
        moment: datetime,
    ) -> None:
        """Open or close a pause interval so it matches the state we just entered."""
        was_paused = previous in CLOCK_PAUSED_STATES
        now_paused = target in CLOCK_PAUSED_STATES

        if now_paused and not was_paused:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clock_pause (application_id, paused_at, reason)
                    VALUES (%s, %s, %s)
                    """,
                    (str(application_id), moment, target.value),
                )
        elif was_paused and not now_paused:
            self._close_open_pause(application_id, moment)
        elif target in TERMINAL_STATES:
            # A case withdrawn while waiting on the applicant must not leave an open
            # pause, or every later report of it would treat the clock as still stopped.
            self._close_open_pause(application_id, moment)

    def _close_open_pause(self, application_id: UUID, moment: datetime) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE clock_pause SET resumed_at = %s
                WHERE application_id = %s AND resumed_at IS NULL
                """,
                (moment, str(application_id)),
            )

    # ------------------------------------------------------------------
    # Application actions
    # ------------------------------------------------------------------

    def create_application(
        self,
        *,
        applicant_id: UUID,
        parcel_id: UUID,
        permit_type_code: str,
        scope_narrative: str,
        declared_valuation: Decimal | float,
        actor: Actor,
        contractor_id: UUID | None = None,
        square_feet: int | None = None,
        occupancy_class: str | None = None,
    ) -> UUID:
        """Create a DRAFT application. The SLA clock does not start here (FR-01)."""
        number = next_application_number(self.conn, self._now().year)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO application (
                    application_number, applicant_id, contractor_id, parcel_id,
                    permit_type_code, status, scope_narrative, declared_valuation,
                    square_feet, occupancy_class, created_by
                )
                VALUES (%s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    number,
                    str(applicant_id),
                    str(contractor_id) if contractor_id else None,
                    str(parcel_id),
                    permit_type_code,
                    scope_narrative,
                    Decimal(str(declared_valuation)),
                    square_feet,
                    occupancy_class,
                    actor.username,
                ),
            )
            application_id = cur.fetchone()["id"]

        audit.record(
            self.conn,
            entity_type="application",
            entity_id=application_id,
            action="create",
            actor=actor.username,
            after={"application_number": number, "permit_type_code": permit_type_code},
        )
        return application_id

    def submit(self, application_id: UUID, actor: Actor) -> ApplicationStatus:
        """DRAFT to SUBMITTED. Starts the SLA clock (FR-01, FR-06)."""
        app = self.get_application(application_id, for_update=True)

        with self.conn.cursor() as cur:
            cur.execute("SELECT stop_work_order FROM parcel WHERE id = %s", (str(app["parcel_id"]),))
            parcel = cur.fetchone()

        if parcel and parcel["stop_work_order"]:
            raise GuardFailed(
                Action.SUBMIT,
                ["parcel has an open stop-work order and cannot accept new permit applications"],
            )

        return self._transition(
            app, Action.SUBMIT, actor, extra_columns={"submitted_at": self._now()}
        )

    def complete_enrichment(self, application_id: UUID, actor: Actor | None = None) -> ApplicationStatus:
        """SUBMITTED to INTAKE_SCREENING once integration enrichment has run.

        Builds the document checklist on the way through, so the intake clerk opens the
        case with the outstanding documents already itemized rather than working from
        memory (FR-04, P5).
        """
        actor = actor or Actor.system()
        app = self.get_application(application_id, for_update=True)
        checklist.build_checklist(self.conn, application_id)
        return self._transition(app, Action.ENRICHMENT_COMPLETE, actor)

    def return_incomplete(
        self, application_id: UUID, actor: Actor, reasons: list[str]
    ) -> ApplicationStatus:
        """Return to the applicant with itemized reasons (FR-05). Pauses the clock."""
        app = self.get_application(application_id, for_update=True)
        if not reasons:
            raise GuardFailed(Action.RETURN_INCOMPLETE, ["at least one itemized reason is required"])
        return self._transition(
            app, Action.RETURN_INCOMPLETE, actor, reason=" | ".join(reasons)
        )

    def accept_intake(
        self,
        application_id: UUID,
        actor: Actor,
        *,
        confirmed_additional_disciplines: list[str] | None = None,
    ) -> ApplicationStatus:
        """Screening complete, open the discipline reviews (FR-04, FR-07, FR-09).

        Refuses while any required document is outstanding, and reports every outstanding
        document rather than the first one.
        """
        app = self.get_application(application_id, for_update=True)

        # Role first, then business validation. An actor who may not perform the action
        # should not be told which documents are outstanding.
        if not role_may(actor.role, Action.ACCEPT_INTAKE):
            raise PermissionDenied(f"role {actor.role} may not {Action.ACCEPT_INTAKE}")

        missing = checklist.missing_document_codes(self.conn, application_id)
        if missing:
            raise GuardFailed(
                Action.ACCEPT_INTAKE,
                [f"required document not received: {code}" for code in missing],
            )

        disciplines = routing.resolve_routing(
            self.conn, app["permit_type_code"], confirmed_additional_disciplines
        )
        if not disciplines:
            raise GuardFailed(Action.ACCEPT_INTAKE, ["no discipline is routed for this permit type"])

        status = self._transition(
            app, Action.ACCEPT_INTAKE, actor, extra_columns={"intake_completed_at": self._now()}
        )
        for discipline in disciplines:
            self._open_task(app, discipline, round_number=1, actor=actor)
        return status

    def resubmit(self, application_id: UUID, actor: Actor) -> ApplicationStatus:
        """Applicant has responded. Resumes the clock (FR-12, FR-15).

        From RETURNED_INCOMPLETE this rebuilds the checklist, because a resubmission that
        raised the declared valuation can pull in a document the first pass did not
        require. From REVISIONS_REQUESTED it opens a new round for the deficient
        disciplines only, leaving the ones that already approved alone.
        """
        app = self.get_application(application_id, for_update=True)
        current = ApplicationStatus(app["status"])

        if current is ApplicationStatus.RETURNED_INCOMPLETE:
            checklist.build_checklist(self.conn, application_id)
            return self._transition(app, Action.RESUBMIT, actor)

        if current is ApplicationStatus.REVISIONS_REQUESTED:
            deficient = self._deficient_disciplines_in_current_round(application_id)
            if not deficient:
                raise GuardFailed(Action.RESUBMIT, ["no deficient discipline to reopen"])
            status = self._transition(app, Action.RESUBMIT, actor)
            for discipline, previous_reviewer_id, previous_round in deficient:
                self._open_task(
                    app,
                    discipline,
                    round_number=previous_round + 1,
                    actor=Actor.system(),
                    preferred_reviewer_id=previous_reviewer_id,
                )
            return status

        raise InvalidTransition("application", current, Action.RESUBMIT)

    def _require_legal(self, application: dict[str, Any], action: Action) -> None:
        """Reject an action the current state does not offer, before any guard runs.

        Structural legality first, then business guards. Told from UNDER_REVIEW that
        "structural review is not complete", a caller would reasonably retry once it is;
        the truth is that issuing is not an action available from that state at all.
        """
        current = ApplicationStatus(application["status"])
        if next_status(current, action) is None:
            raise InvalidTransition("application", current, action)

    def issue(self, application_id: UUID, actor: Actor) -> ApplicationStatus:
        """Issue the permit (FR-18). Refused while any discipline is still open."""
        app = self.get_application(application_id, for_update=True)
        self._require_legal(app, Action.ISSUE)
        open_tasks = self._open_task_disciplines(application_id)
        if open_tasks:
            raise GuardFailed(
                Action.ISSUE,
                [f"discipline review not complete: {d}" for d in open_tasks],
            )
        return self._transition(app, Action.ISSUE, actor, extra_columns={"decided_at": self._now()})

    def deny(self, application_id: UUID, actor: Actor, reason: str) -> ApplicationStatus:
        """Deny the permit (FR-19). A reason is required and is recorded."""
        app = self.get_application(application_id, for_update=True)
        self._require_legal(app, Action.DENY)
        if not reason or not reason.strip():
            raise GuardFailed(Action.DENY, ["a denial requires a recorded reason"])
        return self._transition(
            app, Action.DENY, actor, reason=reason, extra_columns={"decided_at": self._now()}
        )

    def withdraw(self, application_id: UUID, actor: Actor, reason: str | None = None) -> ApplicationStatus:
        """Withdraw the application and cancel any work still open on it."""
        app = self.get_application(application_id, for_update=True)
        status = self._transition(app, Action.WITHDRAW, actor, reason=reason)
        for task in self._open_tasks(application_id):
            self._apply_task_action(task, TaskAction.CANCEL, Actor.system(), reason="application withdrawn")
        return status

    def file_appeal(self, application_id: UUID, actor: Actor, reason: str) -> ApplicationStatus:
        """Freeze a denied case pending an appeals board hearing. Phase 1 records only."""
        app = self.get_application(application_id, for_update=True)
        return self._transition(app, Action.FILE_APPEAL, actor, reason=reason)

    def expire(self, application_id: UUID) -> ApplicationStatus:
        """Close a case the applicant abandoned. Driven by a scheduled job."""
        app = self.get_application(application_id, for_update=True)
        return self._transition(app, Action.EXPIRE, Actor.system(), reason="180 days without applicant response")

    # ------------------------------------------------------------------
    # Review tasks
    # ------------------------------------------------------------------

    def _open_task(
        self,
        application: dict[str, Any],
        discipline_code: str,
        *,
        round_number: int,
        actor: Actor,
        preferred_reviewer_id: UUID | None = None,
    ) -> UUID:
        """Create a review task and assign it if anyone is eligible.

        A task with no eligible reviewer stays PENDING and raises an escalation rather
        than going to whoever happens to be free. An uncertified structural review is a
        worse outcome than a late one.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO review_task (application_id, discipline_code, round, status)
                VALUES (%s, %s, %s, 'PENDING')
                RETURNING id
                """,
                (str(application["id"]), discipline_code, round_number),
            )
            task_id = cur.fetchone()["id"]

        audit.record(
            self.conn,
            entity_type="review_task",
            entity_id=task_id,
            action="open",
            actor=actor.username,
            after={"discipline_code": discipline_code, "round": round_number},
        )

        chosen = self._preferred_if_eligible(discipline_code, preferred_reviewer_id)
        if chosen is None:
            candidate = assignment.pick_reviewer(self.conn, discipline_code)
            chosen = candidate.reviewer_id if candidate else None

        if chosen is None:
            self._raise_escalation(
                application_id=application["id"],
                review_task_id=task_id,
                level="WARNING",
                reason=f"no active certified reviewer available for {discipline_code}",
            )
        else:
            self.assign_task(task_id, chosen, Actor.system())

        return task_id

    def _preferred_if_eligible(self, discipline_code: str, reviewer_id: UUID | None) -> UUID | None:
        """Keep a reopened round with the reviewer who wrote the deficiencies (US-12).

        They already know the case, so returning it to them is both faster and more
        consistent. Falls back to normal assignment when they are no longer active or
        certified.
        """
        if reviewer_id is None:
            return None
        eligible = {c.reviewer_id for c in assignment.eligible_reviewers(self.conn, discipline_code)}
        return reviewer_id if reviewer_id in eligible else None

    def assign_task(self, task_id: UUID, reviewer_id: UUID, actor: Actor) -> TaskStatus:
        task = self.get_task(task_id, for_update=True)
        app = self.get_application(task["application_id"])
        allowance, _ = self._allowance(app["permit_type_code"], "REVIEW_TASK", task["discipline_code"])
        moment = self._now()
        due = add_business_days(moment, allowance, self.calendar)

        return self._apply_task_action(
            task,
            TaskAction.ASSIGN,
            actor,
            extra_columns={
                "reviewer_id": str(reviewer_id),
                "assigned_at": moment,
                "sla_due_at": due,
                "allowance_days": allowance,
            },
        )

    def start_task(self, task_id: UUID, actor: Actor) -> TaskStatus:
        task = self.get_task(task_id, for_update=True)
        self._require_own_task(task, actor)
        return self._apply_task_action(task, TaskAction.START, actor, extra_columns={"started_at": self._now()})

    def approve_task(self, task_id: UUID, actor: Actor, note: str | None = None) -> TaskStatus:
        task = self.get_task(task_id, for_update=True)
        self._require_own_task(task, actor)
        status = self._apply_task_action(
            task, TaskAction.APPROVE, actor, reason=note, extra_columns={"completed_at": self._now()}
        )
        self._resolve_prior_deficiencies(task)
        self._evaluate_review_round(task["application_id"])
        return status

    def approve_task_with_conditions(
        self, task_id: UUID, actor: Actor, conditions: list[str]
    ) -> TaskStatus:
        task = self.get_task(task_id, for_update=True)
        self._require_own_task(task, actor)
        if not conditions:
            raise GuardFailed(TaskAction.APPROVE_WITH_CONDITIONS, ["at least one condition is required"])

        status = self._apply_task_action(
            task, TaskAction.APPROVE_WITH_CONDITIONS, actor, extra_columns={"completed_at": self._now()}
        )
        with self.conn.cursor() as cur:
            for condition in conditions:
                cur.execute(
                    """
                    INSERT INTO review_condition (review_task_id, description, created_by)
                    VALUES (%s, %s, %s)
                    """,
                    (str(task_id), condition, actor.username),
                )
        self._resolve_prior_deficiencies(task)
        self._evaluate_review_round(task["application_id"])
        return status

    def record_deficiencies(
        self, task_id: UUID, actor: Actor, deficiencies: list[dict[str, str]]
    ) -> TaskStatus:
        """Record deficiencies against code provisions (FR-10, FR-11).

        Each entry needs `code_reference`, `description`, and `severity`. The code
        reference is required because FR-19 lets a denial cite a deficiency, and a denial
        citing "structural problems" is not defensible on appeal.
        """
        task = self.get_task(task_id, for_update=True)
        self._require_own_task(task, actor)
        if not deficiencies:
            raise GuardFailed(TaskAction.RECORD_DEFICIENCIES, ["at least one deficiency is required"])

        missing_refs = [d for d in deficiencies if not d.get("code_reference")]
        if missing_refs:
            raise GuardFailed(
                TaskAction.RECORD_DEFICIENCIES,
                ["every deficiency must cite a code provision"],
            )

        status = self._apply_task_action(
            task, TaskAction.RECORD_DEFICIENCIES, actor, extra_columns={"completed_at": self._now()}
        )
        with self.conn.cursor() as cur:
            for item in deficiencies:
                cur.execute(
                    """
                    INSERT INTO deficiency (review_task_id, code_reference, description, severity, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        str(task_id),
                        item["code_reference"],
                        item["description"],
                        item.get("severity", "MAJOR"),
                        actor.username,
                    ),
                )
        self._evaluate_review_round(task["application_id"])
        return status

    def reassign_task(self, task_id: UUID, actor: Actor, reason: str) -> TaskStatus:
        """Move a task back to the queue (FR-13). Supervisor only, reason recorded."""
        task = self.get_task(task_id, for_update=True)
        if not reason or not reason.strip():
            raise GuardFailed(TaskAction.REASSIGN, ["a reassignment requires a recorded reason"])

        previous_reviewer_id = task["reviewer_id"]
        status = self._apply_task_action(
            task,
            TaskAction.REASSIGN,
            actor,
            reason=reason,
            extra_columns={"reviewer_id": None, "assigned_at": None, "sla_due_at": None},
        )

        # Exclude the reviewer being moved off. Without this they are momentarily the
        # least loaded candidate, get picked again, and the reassignment silently does
        # nothing except leave the task unassigned.
        candidate = assignment.pick_reviewer(
            self.conn, task["discipline_code"], exclude=previous_reviewer_id
        )
        if candidate is not None:
            self.assign_task(task_id, candidate.reviewer_id, Actor.system())
        else:
            # The supervisor asked for someone else and the department has nobody. Say so
            # rather than quietly handing it back to the person it was taken from.
            self._raise_escalation(
                application_id=task["application_id"],
                review_task_id=task_id,
                level="WARNING",
                reason=(
                    f"no alternative certified reviewer available for "
                    f"{task['discipline_code']} after reassignment"
                ),
            )
        return status

    def _apply_task_action(
        self,
        task: dict[str, Any],
        action: TaskAction,
        actor: Actor,
        *,
        reason: str | None = None,
        extra_columns: dict[str, Any] | None = None,
    ) -> TaskStatus:
        current = TaskStatus(task["status"])
        target = next_task_status(current, action)
        if target is None:
            raise InvalidTransition("review_task", current, action)
        if not role_may_task(actor.role, action):
            raise PermissionDenied(f"role {actor.role} may not {action}")

        columns = dict(extra_columns or {})
        columns["status"] = target.value
        # Every terminal outcome carries a completion timestamp, enforced by the
        # terminal_has_completed_at check constraint. Stamping it here rather than at each
        # call site means a new terminal action cannot forget it, which is exactly how
        # cancel-on-withdraw got it wrong the first time.
        if target in TERMINAL_TASK_STATUSES and "completed_at" not in columns:
            columns["completed_at"] = self._now()

        assignments = ", ".join(f"{name} = %s" for name in columns)
        params = list(columns.values()) + [str(task["id"])]

        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE review_task SET {assignments} WHERE id = %s", params)

        audit.record(
            self.conn,
            entity_type="review_task",
            entity_id=task["id"],
            action=f"transition:{action}",
            actor=actor.username,
            before={"status": current.value},
            after={"status": target.value, "reason": reason},
        )
        task["status"] = target.value
        return target

    def _require_own_task(self, task: dict[str, Any], actor: Actor) -> None:
        """NFR-01: a reviewer acts only on their own assigned task.

        There is deliberately no supervisor override. A supervisor is not certified in
        every discipline, and letting them sign off a structural review to clear a backlog
        is the same failure FR-09 blocks at assignment time. Their lever is reassignment.
        """
        if actor.reviewer_id is None or task["reviewer_id"] != actor.reviewer_id:
            raise PermissionDenied("review tasks may only be actioned by their assigned reviewer")

    def _resolve_prior_deficiencies(self, task: dict[str, Any]) -> None:
        """Close out deficiencies the earlier round raised for this discipline."""
        if task["round"] <= 1:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE deficiency d
                SET resolved_at = now(), resolved_in_round = %s
                FROM review_task rt
                WHERE d.review_task_id = rt.id
                  AND rt.application_id = %s
                  AND rt.discipline_code = %s
                  AND rt.round < %s
                  AND d.resolved_at IS NULL
                """,
                (task["round"], str(task["application_id"]), task["discipline_code"], task["round"]),
            )

    # ------------------------------------------------------------------
    # Round evaluation
    # ------------------------------------------------------------------

    def _current_round_tasks(self, application_id: UUID) -> list[dict[str, Any]]:
        """Tasks in the latest round per discipline.

        Grouped per discipline rather than taking one global max, because a discipline
        that approved in round 1 has no round 2 row and still has to count as signed off.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (discipline_code) *
                FROM review_task
                WHERE application_id = %s AND status <> 'CANCELLED'
                ORDER BY discipline_code, round DESC
                """,
                (str(application_id),),
            )
            return cur.fetchall()

    def _open_task_disciplines(self, application_id: UUID) -> list[str]:
        return [
            t["discipline_code"]
            for t in self._current_round_tasks(application_id)
            if TaskStatus(t["status"]) in OPEN_TASK_STATUSES
        ]

    def _open_tasks(self, application_id: UUID) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM review_task
                WHERE application_id = %s AND status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
                """,
                (str(application_id),),
            )
            return cur.fetchall()

    def _deficient_disciplines_in_current_round(
        self, application_id: UUID
    ) -> list[tuple[str, UUID | None, int]]:
        return [
            (t["discipline_code"], t["reviewer_id"], t["round"])
            for t in self._current_round_tasks(application_id)
            if TaskStatus(t["status"]) is TaskStatus.DEFICIENT
        ]

    def _evaluate_review_round(self, application_id: UUID) -> None:
        """Advance the parent once every routed discipline has finished this round (FR-08).

        Called after each task outcome. Does nothing until the last one lands, which is
        what makes the disciplines genuinely parallel rather than sequenced.
        """
        app = self.get_application(application_id, for_update=True)
        if ApplicationStatus(app["status"]) is not ApplicationStatus.UNDER_REVIEW:
            return

        tasks = self._current_round_tasks(application_id)
        if not tasks:
            return
        if any(TaskStatus(t["status"]) not in TERMINAL_TASK_STATUSES for t in tasks):
            return

        any_deficient = any(TaskStatus(t["status"]) is TaskStatus.DEFICIENT for t in tasks)
        if any_deficient:
            self._transition(
                app,
                Action.REVIEW_ROUND_DEFICIENT,
                Actor.system(),
                reason="one or more disciplines recorded deficiencies",
            )
        else:
            approved = [t for t in tasks if TaskStatus(t["status"]) in APPROVING_TASK_STATUSES]
            self._transition(
                app,
                Action.REVIEW_ROUND_CLEAN,
                Actor.system(),
                reason=f"{len(approved)} disciplines approved",
            )

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def escalate(self, application_id: UUID, reason: str, level: str = "WARNING") -> None:
        """Raise a case-level escalation for a supervisor.

        Used when something outside the SLA clock needs a human, such as a contractor
        licence coming back revoked. Phase 1 never auto-rejects on that: a revocation that
        turns out to be a data error in someone else's database should not deny a permit
        without a person looking at it.
        """
        self._raise_escalation(
            application_id=application_id, review_task_id=None, level=level, reason=reason
        )

    def _raise_escalation(
        self, *, application_id: UUID, review_task_id: UUID | None, level: str, reason: str
    ) -> None:
        """Insert an escalation, ignoring duplicates at the same level (FR-16).

        The unique indexes in the schema make this safe to call from a job that runs every
        few minutes without producing hundreds of identical rows a day.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO escalation (application_id, review_task_id, level, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (str(application_id), str(review_task_id) if review_task_id else None, level, reason),
            )
