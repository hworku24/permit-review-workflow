"""Domain exceptions.

Split by who is at fault, because the API layer maps them to different status codes and
because a guard failure is a normal business outcome while an integration failure is not.
"""

from __future__ import annotations


class PermitFlowError(Exception):
    """Base for everything raised by this package."""


class NotFound(PermitFlowError):
    """A referenced entity does not exist."""


class InvalidTransition(PermitFlowError):
    """The requested action is not legal from the current state.

    This is a structural error. The action does not exist on this state at all, as
    opposed to existing but being blocked by a guard.
    """

    def __init__(self, entity: str, current: str, action: str) -> None:
        self.entity = entity
        self.current = current
        self.action = action
        super().__init__(f"{action} is not a legal action on {entity} in state {current}")


class GuardFailed(PermitFlowError):
    """The action is legal from this state but a precondition was not met.

    Carries every failed reason rather than the first one. An intake clerk who is told
    only about the first missing document will submit again and be told about the second,
    which is how the current process wastes a week per round trip.
    """

    def __init__(self, action: str, reasons: list[str]) -> None:
        self.action = action
        self.reasons = reasons
        super().__init__(f"{action} refused: " + "; ".join(reasons))


class PermissionDenied(PermitFlowError):
    """The actor's role does not permit this action (NFR-01)."""


class IntegrationError(PermitFlowError):
    """An external system call failed after exhausting retries."""

    def __init__(self, system: str, detail: str) -> None:
        self.system = system
        self.detail = detail
        super().__init__(f"{system}: {detail}")


class CircuitOpen(IntegrationError):
    """The circuit breaker is open, so the call was not attempted (NFR-03)."""

    def __init__(self, system: str, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(system, f"circuit open, retry in {retry_after_seconds:.0f}s")
