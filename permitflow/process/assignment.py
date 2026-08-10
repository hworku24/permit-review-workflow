"""Reviewer assignment (FR-09).

Discovery pain point P4: two of the seven reviewers carried 44% of Q1 volume because
assignment happened in a weekly meeting and nobody could see the distribution. The rule
here is least open tasks first, restricted to reviewers who are both active and certified
in the discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class Candidate:
    reviewer_id: UUID
    username: str
    open_tasks: int


def eligible_reviewers(conn: psycopg.Connection, discipline_code: str) -> list[Candidate]:
    """Active reviewers certified in this discipline, with their current open load.

    Certification and active status are both filters rather than preferences. A task that
    cannot be assigned stays PENDING and is escalated, which is what US-09 asks for. The
    alternative, handing work to whoever is available, is how an uncertified review ends
    up on a permit.

    `open_tasks` counts a reviewer's whole queue, not just this discipline. Someone
    certified in both fire and structural who is buried in structural work is not
    available for a fire review either, and a per-discipline count would say otherwise.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id,
                   r.username,
                   COUNT(rt.id) FILTER (
                       WHERE rt.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
                   ) AS open_tasks
            FROM reviewer r
            JOIN reviewer_discipline rd ON rd.reviewer_id = r.id
            LEFT JOIN review_task rt ON rt.reviewer_id = r.id
            WHERE rd.discipline_code = %s
              AND r.active
              AND r.role = 'reviewer'
            GROUP BY r.id, r.username
            ORDER BY open_tasks, r.username
            """,
            (discipline_code,),
        )
        return [
            Candidate(reviewer_id=r["id"], username=r["username"], open_tasks=r["open_tasks"])
            for r in cur.fetchall()
        ]


def pick_reviewer(
    conn: psycopg.Connection, discipline_code: str, exclude: UUID | None = None
) -> Candidate | None:
    """Least loaded eligible reviewer, or None when nobody is eligible.

    Ties break on username. A deterministic tiebreak is what makes US-09's second
    criterion testable: the same input has to assign the same way every run, otherwise a
    failing assignment test is impossible to reproduce.

    `exclude` drops one reviewer from consideration. Reassignment uses it, because a
    supervisor who moves a task off someone has already decided that person should not
    have it, and unassigning them briefly makes them the least loaded candidate.
    """
    candidates = [c for c in eligible_reviewers(conn, discipline_code) if c.reviewer_id != exclude]
    return candidates[0] if candidates else None
