"""A sign-in gate for deployments that are reachable from anywhere.

The user picker at `/ui/` is how this application has always chosen an actor, and it proves
nothing about who is asking. On a laptop that is the right amount of ceremony. On a public
URL it means anybody who finds the address is a supervisor.

This adds one shared passphrase in front of every screen. It is deliberately not a user
directory: the actors are still the seeded department staff, and the sign-in page says in as
many words that this is a demonstration gate and not the city's single sign-on. Building a
real identity system here would be inventing a requirement, and pretending the picker is
authentication would be worse.

Unset `DEMO_PASSPHRASE` and nothing changes, which is what keeps a fresh clone running with
no credentials at all (NFR-06).
"""

from __future__ import annotations

import hmac
import secrets
import time
from hashlib import sha256

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import get_settings

GATE_COOKIE = "permitflow_gate"

#: Reachable without signing in. `/health` is what the load balancer calls, and it reports
#: only that the database and the corpus are readable.
OPEN_PATHS = frozenset({"/health", "/ui/gate", "/ui/static/app.css"})


def _secret() -> bytes:
    settings = get_settings()
    return (settings.session_secret or _process_secret()).encode()


_PROCESS_SECRET = secrets.token_urlsafe(32)


def _process_secret() -> str:
    """Used when no secret is configured. Restarting signs everyone out, which is a
    reasonable default for a demonstration and a bad one for a deployment, so a deployment
    sets SESSION_SECRET."""
    return _PROCESS_SECRET


def issue(expires_at: int) -> str:
    """A cookie value that carries its own expiry and a signature over it."""
    payload = str(expires_at)
    signature = hmac.new(_secret(), payload.encode(), sha256).hexdigest()
    return f"{payload}.{signature}"


def valid(token: str | None) -> bool:
    """Constant-time check of the signature, then the expiry.

    Comparing with `==` would leak the signature a byte at a time to anyone willing to
    measure. It costs nothing to use the right comparison.
    """
    if not token or "." not in token:
        return False
    payload, _, signature = token.partition(".")
    expected = hmac.new(_secret(), payload.encode(), sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        return int(payload) > int(time.time())
    except ValueError:
        return False


class GateMiddleware(BaseHTTPMiddleware):
    """Refuses every request without a valid gate cookie once a passphrase is configured."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.demo_passphrase:
            return await call_next(request)

        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/ui/static/"):
            return await call_next(request)

        if valid(request.cookies.get(GATE_COOKIE)):
            return await call_next(request)

        if path.startswith("/ui"):
            return RedirectResponse(url="/ui/gate", status_code=303)
        # The API is not a browser, so it gets a status code and not a redirect.
        return HTMLResponse("sign in at /ui/gate", status_code=401)
