"""Actor resolution and role checks (NFR-01).

The caller identifies themselves with an `X-Actor` header naming a department username.
A username in the `reviewer` table resolves to that person's role and reviewer id; anything
else is treated as an applicant acting on their own behalf.

This is not authentication. A real deployment puts the city's SSO in front of this and
resolves the actor from a validated token. It is called out here rather than hidden
because a header-based identity that looks like auth is worse than one that obviously
is not, and the engine's authorization is the part being demonstrated: every action is
checked against the actor's role in `process/states.py`, no matter how the actor arrived.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from ..db import read_connection
from ..process.engine import Actor
from ..process.states import Role


def resolve_actor(x_actor: Annotated[str | None, Header()] = None) -> Actor:
    if not x_actor:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Actor header is required",
        )

    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, role, active FROM reviewer WHERE username = %s",
            (x_actor,),
        )
        row = cur.fetchone()

    if row is None:
        # Not staff. Applicants are external and are not rows in the reviewer table.
        return Actor(username=x_actor, role=Role.APPLICANT)

    if not row["active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{x_actor} is not an active user",
        )

    return Actor(username=row["username"], role=Role(row["role"]), reviewer_id=row["id"])


CurrentActor = Annotated[Actor, Depends(resolve_actor)]


def require_roles(*roles: Role):
    """Dependency factory for endpoints only certain roles may reach at all.

    Most authorization lives in the engine, which is the right place for it: a rule
    enforced at the API boundary is a rule a script driving the engine directly can skip.
    This exists for the handful of endpoints whose very shape is role-specific, such as a
    reviewer's own queue.
    """

    def check(actor: CurrentActor) -> Actor:
        if actor.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {actor.role} may not use this endpoint",
            )
        return actor

    return check
