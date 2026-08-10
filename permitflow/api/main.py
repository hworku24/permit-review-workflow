"""FastAPI application.

Domain errors are mapped to status codes here rather than raised as HTTPException from
inside the engine, which keeps the engine usable from a script or a test without dragging
in a web framework.

The mapping matters more than it looks. An invalid transition is a 409, because the action
does not exist on the current state and retrying will not help. A guard failure is a 422
carrying every failed reason, because it is a normal business outcome the caller can fix.
Reporting only the first missing document is how the department's current process wastes a
week per round trip, so the API never does that.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import (
    CircuitOpen,
    GuardFailed,
    IntegrationError,
    InvalidTransition,
    NotFound,
    PermissionDenied,
)
from .routes import ai, applications, queues, reports, tasks

app = FastAPI(
    title="PermitFlow",
    version="0.1.0",
    summary="Building permit case management for the City of Rivermont",
    description=(
        "Phase 1 covers intake through issuance. Callers identify themselves with an "
        "`X-Actor` header naming a department username; see permitflow/api/security.py "
        "for why that is not authentication."
    ),
)

app.include_router(applications.router)
app.include_router(tasks.router)
app.include_router(queues.router)
app.include_router(ai.router)
app.include_router(reports.router)


@app.exception_handler(InvalidTransition)
def _invalid_transition(request: Request, exc: InvalidTransition) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "current_state": exc.current,
            "attempted_action": exc.action,
            "reasons": [],
        },
    )


@app.exception_handler(GuardFailed)
def _guard_failed(request: Request, exc: GuardFailed) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": f"{exc.action} refused", "reasons": exc.reasons},
    )


@app.exception_handler(PermissionDenied)
def _permission_denied(request: Request, exc: PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc), "reasons": []})


@app.exception_handler(NotFound)
def _not_found(request: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "reasons": []})


@app.exception_handler(CircuitOpen)
def _circuit_open(request: Request, exc: CircuitOpen) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "reasons": []},
        headers={"Retry-After": str(int(exc.retry_after_seconds))},
    )


@app.exception_handler(IntegrationError)
def _integration_error(request: Request, exc: IntegrationError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc), "reasons": []})


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness plus the two things most likely to be broken in a fresh environment."""
    from ..ai.retrieval import load_index
    from ..db import read_connection

    checks: dict[str, str] = {}
    try:
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM permit_type")
            checks["database"] = f"ok, {cur.fetchone()['n']} permit types"
    except Exception as exc:  # noqa: BLE001 - health endpoint reports rather than raises
        checks["database"] = f"unavailable: {exc}"

    try:
        checks["ordinance_corpus"] = f"ok, {len(load_index())} sections"
    except Exception as exc:  # noqa: BLE001
        checks["ordinance_corpus"] = f"unavailable: {exc}"

    healthy = all(v.startswith("ok") for v in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}
