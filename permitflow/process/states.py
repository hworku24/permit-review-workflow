"""The lifecycle contract.

This module is the single source of truth for which actions are legal from which states.
It holds no database access and no guard logic on purpose: guards are conditions that can
fail at runtime, while this table is structural and can be read, diffed, and argued about
in a client workshop. `docs/02-process-map.md` renders the same table as a diagram.
"""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    INTAKE_SCREENING = "INTAKE_SCREENING"
    RETURNED_INCOMPLETE = "RETURNED_INCOMPLETE"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVISIONS_REQUESTED = "REVISIONS_REQUESTED"
    PENDING_DECISION = "PENDING_DECISION"
    ISSUED = "ISSUED"
    DENIED = "DENIED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"
    APPEAL_FILED = "APPEAL_FILED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    APPROVED_WITH_CONDITIONS = "APPROVED_WITH_CONDITIONS"
    DEFICIENT = "DEFICIENT"
    CANCELLED = "CANCELLED"


class Action(StrEnum):
    # Applicant
    SUBMIT = "submit"
    RESUBMIT = "resubmit"
    WITHDRAW = "withdraw"
    FILE_APPEAL = "file_appeal"
    # Intake clerk
    ACCEPT_INTAKE = "accept_intake"
    RETURN_INCOMPLETE = "return_incomplete"
    # Supervisor
    ISSUE = "issue"
    DENY = "deny"
    # System
    ENRICHMENT_COMPLETE = "enrichment_complete"
    REVIEW_ROUND_CLEAN = "review_round_clean"
    REVIEW_ROUND_DEFICIENT = "review_round_deficient"
    EXPIRE = "expire"


class TaskAction(StrEnum):
    ASSIGN = "assign"
    START = "start"
    REASSIGN = "reassign"
    APPROVE = "approve"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    RECORD_DEFICIENCIES = "record_deficiencies"
    CANCEL = "cancel"


class Role(StrEnum):
    APPLICANT = "applicant"
    INTAKE_CLERK = "intake_clerk"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Application transitions
# ---------------------------------------------------------------------------

APPLICATION_TRANSITIONS: dict[tuple[ApplicationStatus, Action], ApplicationStatus] = {
    (ApplicationStatus.DRAFT, Action.SUBMIT): ApplicationStatus.SUBMITTED,
    (ApplicationStatus.SUBMITTED, Action.ENRICHMENT_COMPLETE): ApplicationStatus.INTAKE_SCREENING,

    (ApplicationStatus.INTAKE_SCREENING, Action.RETURN_INCOMPLETE): ApplicationStatus.RETURNED_INCOMPLETE,
    (ApplicationStatus.RETURNED_INCOMPLETE, Action.RESUBMIT): ApplicationStatus.INTAKE_SCREENING,
    (ApplicationStatus.INTAKE_SCREENING, Action.ACCEPT_INTAKE): ApplicationStatus.UNDER_REVIEW,

    (ApplicationStatus.UNDER_REVIEW, Action.REVIEW_ROUND_DEFICIENT): ApplicationStatus.REVISIONS_REQUESTED,
    (ApplicationStatus.UNDER_REVIEW, Action.REVIEW_ROUND_CLEAN): ApplicationStatus.PENDING_DECISION,
    (ApplicationStatus.REVISIONS_REQUESTED, Action.RESUBMIT): ApplicationStatus.UNDER_REVIEW,

    (ApplicationStatus.PENDING_DECISION, Action.ISSUE): ApplicationStatus.ISSUED,
    (ApplicationStatus.PENDING_DECISION, Action.DENY): ApplicationStatus.DENIED,

    (ApplicationStatus.DRAFT, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.SUBMITTED, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.INTAKE_SCREENING, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.RETURNED_INCOMPLETE, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.UNDER_REVIEW, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.REVISIONS_REQUESTED, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,
    (ApplicationStatus.PENDING_DECISION, Action.WITHDRAW): ApplicationStatus.WITHDRAWN,

    (ApplicationStatus.RETURNED_INCOMPLETE, Action.EXPIRE): ApplicationStatus.EXPIRED,
    (ApplicationStatus.REVISIONS_REQUESTED, Action.EXPIRE): ApplicationStatus.EXPIRED,

    (ApplicationStatus.DENIED, Action.FILE_APPEAL): ApplicationStatus.APPEAL_FILED,
}

# ---------------------------------------------------------------------------
# Review task transitions
#
# DEFICIENT has no outgoing transition here. FR-12 reopens a deficient discipline by
# inserting a new row at round N+1 rather than by moving the existing row backwards, so
# the round 1 record stays readable. The arrow in the process map diagram labelled
# "parent resubmitted" is that insert, not a transition on this table.
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskAction], TaskStatus] = {
    (TaskStatus.PENDING, TaskAction.ASSIGN): TaskStatus.ASSIGNED,
    (TaskStatus.ASSIGNED, TaskAction.START): TaskStatus.IN_PROGRESS,

    (TaskStatus.ASSIGNED, TaskAction.REASSIGN): TaskStatus.PENDING,
    (TaskStatus.IN_PROGRESS, TaskAction.REASSIGN): TaskStatus.PENDING,

    (TaskStatus.IN_PROGRESS, TaskAction.APPROVE): TaskStatus.APPROVED,
    (TaskStatus.IN_PROGRESS, TaskAction.APPROVE_WITH_CONDITIONS): TaskStatus.APPROVED_WITH_CONDITIONS,
    (TaskStatus.IN_PROGRESS, TaskAction.RECORD_DEFICIENCIES): TaskStatus.DEFICIENT,

    (TaskStatus.PENDING, TaskAction.CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.ASSIGNED, TaskAction.CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.IN_PROGRESS, TaskAction.CANCEL): TaskStatus.CANCELLED,
}

# ---------------------------------------------------------------------------
# Role permissions (NFR-01)
# ---------------------------------------------------------------------------

ACTION_ROLES: dict[Action, frozenset[Role]] = {
    Action.SUBMIT: frozenset({Role.APPLICANT, Role.INTAKE_CLERK}),
    Action.RESUBMIT: frozenset({Role.APPLICANT, Role.INTAKE_CLERK}),
    Action.WITHDRAW: frozenset({Role.APPLICANT, Role.SUPERVISOR}),
    Action.FILE_APPEAL: frozenset({Role.APPLICANT}),
    Action.ACCEPT_INTAKE: frozenset({Role.INTAKE_CLERK, Role.SUPERVISOR}),
    Action.RETURN_INCOMPLETE: frozenset({Role.INTAKE_CLERK, Role.SUPERVISOR}),
    Action.ISSUE: frozenset({Role.SUPERVISOR}),
    Action.DENY: frozenset({Role.SUPERVISOR}),
    Action.ENRICHMENT_COMPLETE: frozenset({Role.SYSTEM}),
    Action.REVIEW_ROUND_CLEAN: frozenset({Role.SYSTEM}),
    Action.REVIEW_ROUND_DEFICIENT: frozenset({Role.SYSTEM}),
    Action.EXPIRE: frozenset({Role.SYSTEM}),
}

TASK_ACTION_ROLES: dict[TaskAction, frozenset[Role]] = {
    TaskAction.ASSIGN: frozenset({Role.SYSTEM, Role.SUPERVISOR}),
    TaskAction.START: frozenset({Role.REVIEWER}),
    TaskAction.REASSIGN: frozenset({Role.SUPERVISOR}),
    TaskAction.APPROVE: frozenset({Role.REVIEWER}),
    TaskAction.APPROVE_WITH_CONDITIONS: frozenset({Role.REVIEWER}),
    TaskAction.RECORD_DEFICIENCIES: frozenset({Role.REVIEWER}),
    TaskAction.CANCEL: frozenset({Role.SYSTEM, Role.SUPERVISOR}),
}

# ---------------------------------------------------------------------------
# State sets
# ---------------------------------------------------------------------------

#: States where the department is waiting on the applicant. The SLA clock is stopped in
#: these, which is the whole reason clock_pause exists (FR-15).
CLOCK_PAUSED_STATES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.RETURNED_INCOMPLETE,
    ApplicationStatus.REVISIONS_REQUESTED,
})

TERMINAL_STATES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.ISSUED,
    ApplicationStatus.DENIED,
    ApplicationStatus.WITHDRAWN,
    ApplicationStatus.EXPIRED,
    ApplicationStatus.APPEAL_FILED,
})

#: A task in one of these has finished its round. FR-08 waits for every routed discipline
#: to land here before the parent application moves.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.APPROVED,
    TaskStatus.APPROVED_WITH_CONDITIONS,
    TaskStatus.DEFICIENT,
    TaskStatus.CANCELLED,
})

OPEN_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.PENDING,
    TaskStatus.ASSIGNED,
    TaskStatus.IN_PROGRESS,
})

#: Outcomes that count as the discipline having signed off.
APPROVING_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.APPROVED,
    TaskStatus.APPROVED_WITH_CONDITIONS,
})


def next_status(current: ApplicationStatus, action: Action) -> ApplicationStatus | None:
    """Target state for an action, or None when the action is not legal here."""
    return APPLICATION_TRANSITIONS.get((current, action))


def next_task_status(current: TaskStatus, action: TaskAction) -> TaskStatus | None:
    return TASK_TRANSITIONS.get((current, action))


def role_may(role: Role, action: Action) -> bool:
    return role in ACTION_ROLES.get(action, frozenset())


def role_may_task(role: Role, action: TaskAction) -> bool:
    return role in TASK_ACTION_ROLES.get(action, frozenset())
