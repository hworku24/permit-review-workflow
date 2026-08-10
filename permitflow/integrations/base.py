"""Shared resilience policy for outbound integrations (NFR-02, NFR-03).

Both external systems belong to other governments. Neither has an SLA to Rivermont, and
neither can be asked to change. The policy here is the same for both: bounded retry with
exponential backoff, a circuit breaker so a sustained outage stops costing every applicant
three timeouts, and an attempt-level log so an integration dispute can be settled with
records instead of recollection.

The important design decision is that failure is not fatal. A county outage must not stop
Rivermont accepting permit applications, so callers get a result object that says whether
the lookup succeeded, and the case proceeds either way with the gap recorded.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar
from uuid import UUID

from psycopg.types.json import Jsonb

from ..config import get_settings
from ..errors import CircuitOpen, IntegrationError

T = TypeVar("T")


@dataclass
class CircuitBreaker:
    """Stops calling a dependency that is clearly down.

    Without this, an outage costs every single applicant the full retry budget before
    their application is accepted. With it, the first few callers pay that cost and
    everyone after them fails fast and gets queued immediately.
    """

    failure_threshold: int
    reset_seconds: float
    clock: Callable[[], float] = time.monotonic
    consecutive_failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self.clock() - self.opened_at >= self.reset_seconds:
            # Half open: let the next call through to test the water. A success closes
            # the circuit, a failure re-opens it for another full interval.
            self.opened_at = None
            self.consecutive_failures = 0
            return False
        return True

    def retry_after(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.reset_seconds - (self.clock() - self.opened_at))

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self.clock()


@dataclass
class CallOutcome(Generic[T]):
    """What happened, whether or not it worked.

    Callers branch on `verified`. The distinction between "checked and it failed" and
    "could not check" is carried all the way through, because collapsing the two is how
    an outage quietly waves a revoked contractor licence through intake.
    """

    verified: bool
    value: T | None = None
    status: str = "SUCCESS"
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return not self.verified


@dataclass
class ResiliencePolicy:
    max_attempts: int
    backoff_base_seconds: float
    breaker: CircuitBreaker
    sleep: Callable[[float], None] = time.sleep
    #: Overridden in tests so a delay assertion does not cost wall-clock seconds.
    attempts_made: list[float] = field(default_factory=list)

    @classmethod
    def from_settings(cls, **overrides: Any) -> ResiliencePolicy:
        s = get_settings()
        defaults = {
            "max_attempts": s.integration_max_attempts,
            "backoff_base_seconds": s.integration_backoff_base_seconds,
            "breaker": CircuitBreaker(
                failure_threshold=s.circuit_failure_threshold,
                reset_seconds=s.circuit_reset_seconds,
            ),
        }
        return cls(**(defaults | overrides))


def log_call(
    *,
    system: str,
    operation: str,
    attempt: int,
    status: str,
    application_id: UUID | None = None,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    error_detail: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record one attempt.

    Deliberately writes on its own autocommit connection rather than joining the caller's
    transaction. A failed lookup usually ends in the caller rolling something back, and an
    attempt log that disappears exactly when the call failed is worse than no log at all.
    """
    from ..db import get_pool

    try:
        with get_pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_call (
                        application_id, system, operation, attempt, status,
                        request_payload, response_payload, error_detail, latency_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(application_id) if application_id else None,
                        system,
                        operation,
                        attempt,
                        status,
                        Jsonb(request_payload) if request_payload is not None else None,
                        Jsonb(response_payload) if response_payload is not None else None,
                        error_detail,
                        latency_ms,
                    ),
                )
    except Exception:
        # Logging an attempt must never be the reason an integration fails. The call
        # itself already succeeded or failed on its own merits.
        pass


def call_with_resilience(
    *,
    system: str,
    operation: str,
    fn: Callable[[], T],
    policy: ResiliencePolicy,
    is_retryable: Callable[[Exception], bool],
    classify: Callable[[Exception], str],
    application_id: UUID | None = None,
    request_payload: dict[str, Any] | None = None,
    response_summary: Callable[[T], dict[str, Any]] | None = None,
) -> T:
    """Run `fn`, retrying transient failures, and raise IntegrationError if it never works.

    Non-retryable failures (a parcel that genuinely does not exist) fail on the first
    attempt. Retrying a definitive answer is just three times the latency for the same
    result.
    """
    if policy.breaker.is_open:
        log_call(
            system=system, operation=operation, attempt=0, status="CIRCUIT_OPEN",
            application_id=application_id, request_payload=request_payload,
            error_detail="circuit breaker open, call not attempted",
        )
        raise CircuitOpen(system, policy.breaker.retry_after())

    last_error: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - classified below, then re-raised
            elapsed_ms = int((time.monotonic() - started) * 1000)
            status = classify(exc)
            log_call(
                system=system, operation=operation, attempt=attempt, status=status,
                application_id=application_id, request_payload=request_payload,
                error_detail=str(exc)[:500], latency_ms=elapsed_ms,
            )
            last_error = exc

            if not is_retryable(exc):
                policy.breaker.record_success()  # A definitive answer is not an outage.
                raise IntegrationError(system, str(exc)) from exc

            policy.breaker.record_failure()
            if attempt < policy.max_attempts:
                delay = policy.backoff_base_seconds * (2 ** (attempt - 1))
                policy.attempts_made.append(delay)
                policy.sleep(delay)
            continue

        elapsed_ms = int((time.monotonic() - started) * 1000)
        policy.breaker.record_success()
        log_call(
            system=system, operation=operation, attempt=attempt, status="SUCCESS",
            application_id=application_id, request_payload=request_payload,
            response_payload=response_summary(result) if response_summary else None,
            latency_ms=elapsed_ms,
        )
        return result

    raise IntegrationError(system, f"failed after {policy.max_attempts} attempts: {last_error}")
