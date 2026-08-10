"""Review task endpoints. Reviewers act only on their own tasks (NFR-01)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ...db import transaction
from ...process.engine import Engine
from ..schemas import ApproveWithConditions, Reassign, RecordDeficiencies, TaskNote
from ..security import CurrentActor

router = APIRouter(prefix="/tasks", tags=["review"])


@router.post("/{task_id}/start")
def start(task_id: UUID, actor: CurrentActor) -> dict:
    with transaction() as conn:
        return {"status": Engine(conn).start_task(task_id, actor).value}


@router.post("/{task_id}/approve")
def approve(task_id: UUID, body: TaskNote, actor: CurrentActor) -> dict:
    """Approve this discipline. Advances the parent only when every discipline is done."""
    with transaction() as conn:
        engine = Engine(conn)
        task_status = engine.approve_task(task_id, actor, body.note)
        parent = engine.get_application(engine.get_task(task_id)["application_id"])
        return {"task_status": task_status.value, "application_status": parent["status"]}


@router.post("/{task_id}/approve-with-conditions")
def approve_with_conditions(
    task_id: UUID, body: ApproveWithConditions, actor: CurrentActor
) -> dict:
    with transaction() as conn:
        engine = Engine(conn)
        task_status = engine.approve_task_with_conditions(task_id, actor, body.conditions)
        parent = engine.get_application(engine.get_task(task_id)["application_id"])
        return {
            "task_status": task_status.value,
            "application_status": parent["status"],
            "conditions": body.conditions,
        }


@router.post("/{task_id}/deficiencies")
def record_deficiencies(task_id: UUID, body: RecordDeficiencies, actor: CurrentActor) -> dict:
    """Record deficiencies against code provisions (FR-10, FR-11).

    Every deficiency cites a provision. FR-19 lets a denial rest on these, and a denial
    citing "structural problems" would not survive an appeal.
    """
    with transaction() as conn:
        engine = Engine(conn)
        task_status = engine.record_deficiencies(
            task_id, actor, [d.model_dump() for d in body.deficiencies]
        )
        parent = engine.get_application(engine.get_task(task_id)["application_id"])
        return {
            "task_status": task_status.value,
            "application_status": parent["status"],
            "deficiency_count": len(body.deficiencies),
        }


@router.post("/{task_id}/reassign")
def reassign(task_id: UUID, body: Reassign, actor: CurrentActor) -> dict:
    """Send a task back to the queue and reassign it (FR-13). Supervisor only."""
    with transaction() as conn:
        engine = Engine(conn)
        engine.reassign_task(task_id, actor, body.reason)
        task = engine.get_task(task_id)
        with conn.cursor() as cur:
            cur.execute("SELECT full_name FROM reviewer WHERE id = %s", (str(task["reviewer_id"]),))
            row = cur.fetchone()
        return {
            "task_status": task["status"],
            "reviewer": row["full_name"] if row else None,
            "reason": body.reason,
        }
