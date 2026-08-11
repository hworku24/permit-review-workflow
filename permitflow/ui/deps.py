"""Who the browser is acting as.

A browser cannot set the `X-Actor` header the API uses, so the screens carry the same idea
in a cookie the picker at `/ui/` writes. It is the same non-authentication for the same
reason, and it is worth being blunt about: anyone can edit a cookie. The city's SSO goes in
front of this before it is reachable from anywhere but a laptop, and the engine still checks
every action against the actor's role no matter how the actor arrived.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.responses import RedirectResponse

from ..db import read_connection
from ..process.engine import Actor
from ..process.states import Role

ACTOR_COOKIE = "permitflow_actor"


class NotSignedIn(Exception):
    """Raised when no usable actor cookie is present. Handled as a redirect to the picker."""


def staff_directory() -> list[dict]:
    """Everyone the picker offers, active first."""
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.username, r.full_name, r.role, r.active,
                   count(*) FILTER (
                       WHERE t.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
                   ) AS open_tasks
            FROM reviewer r
            LEFT JOIN review_task t ON t.reviewer_id = r.id
            GROUP BY r.username, r.full_name, r.role, r.active
            ORDER BY r.active DESC, r.role, r.username
            """
        )
        return cur.fetchall()


def current_actor(request: Request) -> Actor:
    """Resolve the cookie to an actor, or raise so the caller is sent to the picker.

    An inactive account raises and does not resolve. Tomas Brandt is seeded inactive
    precisely so there is one account that proves the check runs.
    """
    username = request.cookies.get(ACTOR_COOKIE)
    if not username:
        raise NotSignedIn

    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, role, active FROM reviewer WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()

    if row is None or not row["active"]:
        raise NotSignedIn

    return Actor(username=row["username"], role=Role(row["role"]), reviewer_id=row["id"])


CurrentUser = Annotated[Actor, Depends(current_actor)]


def to_picker(request: Request) -> RedirectResponse:
    """Send an unidentified browser to the picker, remembering where it was going."""
    return RedirectResponse(url=f"/ui/?next={request.url.path}", status_code=303)
