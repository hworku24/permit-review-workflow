"""State contractor licensing client (integration I2, direct database connection).

The state exposes no API. Jurisdictions get a read only account against a replica, so this
is hand-written SQL over a connection to a schema Rivermont does not own and cannot
change. In an Appian implementation this is a JDBC data source; the shape is identical, a
connection string plus a pool plus a read only credential. See docs/04-integration-spec.md
section I2.

The rule that matters most here is the one at the bottom. `UNVERIFIED` is a distinct
outcome from `EXPIRED`, and neither of them is `ACTIVE`. Collapsing "could not check" into
"checked and it passed" is how a database outage quietly waves a revoked licence through
intake, and it is the kind of defect that only surfaces during an appeal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import psycopg

from ..config import get_settings
from .base import CallOutcome, ResiliencePolicy, call_with_resilience

SYSTEM = "BUSINESS_LICENSING"
OPERATION = "SELECT contractor_license"

#: How close to expiry a licence has to be before the clerk is warned.
EXPIRY_WARNING_WINDOW = timedelta(days=30)


class LicenseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    NOT_FOUND = "NOT_FOUND"
    UNVERIFIED = "UNVERIFIED"


class IntakeEffect(StrEnum):
    """What the licence result does to the application."""

    PROCEED = "PROCEED"
    PROCEED_WITH_WARNING = "PROCEED_WITH_WARNING"
    RAISE_DEFICIENCY = "RAISE_DEFICIENCY"
    ESCALATE_TO_SUPERVISOR = "ESCALATE_TO_SUPERVISOR"


@dataclass(frozen=True)
class LicenseRecord:
    license_number: str
    business_name: str
    license_type: str
    status: LicenseStatus
    issued_on: date | None
    expires_on: date | None
    disciplinary_action_count: int


def _is_retryable(exc: Exception) -> bool:
    """Connection problems are worth retrying. A malformed query is not."""
    return isinstance(exc, (psycopg.OperationalError, OSError))


def _classify(exc: Exception) -> str:
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    if isinstance(exc, psycopg.OperationalError):
        return "ERROR"
    return "ERROR"


class LicensingClient:
    #: Identifies which backend answered, so the integration log says how the licence was
    #: reached and not only what it said.
    name = "direct"

    def __init__(
        self,
        dsn: str | None = None,
        timeout: float | None = None,
        policy: ResiliencePolicy | None = None,
    ) -> None:
        settings = get_settings()
        self.dsn = dsn or settings.licensing_db_url
        self.timeout = timeout or settings.licensing_db_timeout_seconds
        self.policy = policy or ResiliencePolicy.from_settings()

    def _query(self, license_number: str) -> dict | None:
        # Short-lived connection rather than a pool. Intake lookups are infrequent and a
        # pooled connection to someone else's replica is a connection held open against a
        # system Rivermont does not operate.
        with psycopg.connect(self.dsn, connect_timeout=int(self.timeout)) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT license_number, business_name, license_type, status,
                           issued_on, expires_on, disciplinary_action_count
                    FROM licensing.contractor_license
                    WHERE license_number = %s
                    """,
                    (license_number,),
                )
                return cur.fetchone()

    def verify(self, license_number: str, application_id: UUID | None = None) -> CallOutcome[LicenseRecord]:
        """Look up a licence. Never raises for a connection problem.

        A row that is absent is a verified answer of NOT_FOUND. A database that cannot be
        reached is an unverified outcome. Those are different things and the caller is
        told which one happened.
        """
        try:
            row = call_with_resilience(
                system=SYSTEM,
                operation=OPERATION,
                fn=lambda: self._query(license_number),
                policy=self.policy,
                is_retryable=_is_retryable,
                classify=_classify,
                application_id=application_id,
                request_payload={"license_number": license_number},
                response_summary=lambda r: {"status": r["status"] if r else "NOT_FOUND"},
            )
        except Exception as exc:  # noqa: BLE001 - degrade, per NFR-02
            return CallOutcome(verified=False, status="UNVERIFIED", detail=str(exc)[:300])

        if row is None:
            return CallOutcome(
                verified=True,
                value=None,
                status=LicenseStatus.NOT_FOUND,
                detail=f"no licence on record for {license_number}",
            )

        # char(12) pads with spaces. Stripping here rather than at every call site.
        return CallOutcome(
            verified=True,
            value=LicenseRecord(
                license_number=row["license_number"].strip(),
                business_name=row["business_name"].strip(),
                license_type=row["license_type"].strip(),
                status=_parse_status(row["status"]),
                issued_on=row["issued_on"],
                expires_on=row["expires_on"],
                disciplinary_action_count=row["disciplinary_action_count"] or 0,
            ),
        )

    def verification(
        self,
        license_number: str,
        as_of: date | None = None,
        application_id: UUID | None = None,
    ) -> Verification:
        """The `LicensingBackend` view of a lookup: the answer plus what intake does about it.

        `verify` reports what the replica said and whether it could be reached.
        `intake_effect` decides what that means for the application. Keeping the two apart
        is what lets the effect rules be tested without a database.
        """
        outcome = self.verify(license_number, application_id=application_id)
        status, effect, message = intake_effect(outcome, as_of=as_of)
        return Verification(
            license_number=license_number,
            verified=outcome.verified,
            status=status,
            effect=effect,
            message=message,
            business_name=outcome.value.business_name if outcome.value else None,
            expires_on=outcome.value.expires_on if outcome.value else None,
        )


def _parse_status(raw: str | None) -> LicenseStatus:
    """Map the replica's status text onto a known value.

    The column is an unconstrained varchar, so a value nobody anticipated is a real
    possibility. An unrecognised status becomes UNVERIFIED rather than raising, because a
    single odd row in someone else's database should not take down intake, and it must
    certainly not be guessed at as ACTIVE.
    """
    if raw is None:
        return LicenseStatus.UNVERIFIED
    try:
        return LicenseStatus(raw.strip().upper())
    except ValueError:
        return LicenseStatus.UNVERIFIED


def intake_effect(outcome: CallOutcome[LicenseRecord], as_of: date | None = None) -> tuple[LicenseStatus, IntakeEffect, str]:
    """Map a licence lookup onto what intake should do about it.

    Suspended and revoked escalate rather than auto-rejecting. A revocation that turns out
    to be a data error in someone else's database should not deny a permit without a human
    looking at it, which is the same principle as AI-04 applied to an integration.
    """
    as_of = as_of or date.today()

    if not outcome.verified:
        return (
            LicenseStatus.UNVERIFIED,
            IntakeEffect.RAISE_DEFICIENCY,
            "contractor licence could not be verified, the check must be repeated before issuance",
        )

    if outcome.value is None:
        return (
            LicenseStatus.NOT_FOUND,
            IntakeEffect.RAISE_DEFICIENCY,
            "no contractor licence found for the number supplied",
        )

    record = outcome.value

    # A status this code cannot interpret stops here rather than falling through to the
    # expiry checks below, which would classify an unrecognised licence as active purely
    # because its expiry date happens to be in the future.
    if record.status is LicenseStatus.UNVERIFIED:
        return (
            LicenseStatus.UNVERIFIED,
            IntakeEffect.RAISE_DEFICIENCY,
            "contractor licence status is not recognised, manual check required",
        )

    if record.status in (LicenseStatus.SUSPENDED, LicenseStatus.REVOKED):
        return (
            record.status,
            IntakeEffect.ESCALATE_TO_SUPERVISOR,
            f"contractor licence is {record.status.lower()}, supervisor review required",
        )

    if record.status is LicenseStatus.EXPIRED:
        return (
            record.status,
            IntakeEffect.RAISE_DEFICIENCY,
            f"contractor licence expired on {record.expires_on}",
        )

    if record.expires_on is not None and record.expires_on <= as_of:
        # The replica's status column lags. Trust the date over the label.
        return (
            LicenseStatus.EXPIRED,
            IntakeEffect.RAISE_DEFICIENCY,
            f"contractor licence expired on {record.expires_on}",
        )

    if record.expires_on is not None and record.expires_on - as_of <= EXPIRY_WARNING_WINDOW:
        return (
            LicenseStatus.ACTIVE,
            IntakeEffect.PROCEED_WITH_WARNING,
            f"contractor licence expires on {record.expires_on}, inside the 30 day window",
        )

    return (LicenseStatus.ACTIVE, IntakeEffect.PROCEED, "contractor licence is active")


@dataclass(frozen=True)
class Verification:
    """One licence answer, whichever backend produced it.

    `verified` answers "did we reach the state's replica", separately from `status`, which
    answers "what did it say". Keeping them apart is what stops an outage being mistaken
    for a pass.
    """

    license_number: str
    verified: bool
    status: LicenseStatus
    effect: IntakeEffect
    message: str
    business_name: str | None = None
    expires_on: date | None = None


class LicensingBackend(Protocol):
    """How the department reaches the state's licensing data.

    The seam exists because the same verification is implemented twice against the same
    replica schema. `LicensingClient` below connects directly and is the path this
    application takes today. `licensing-verifier/` is a Spring service holding the same
    rules over a JDBC datasource, which is the shape the state actually grants access in.

    Only the direct backend is wired in. An HTTP backend calling the Java service, and a
    parity test driving both over the same data, are the next piece of work here.
    """

    name: str

    def verification(self, license_number: str, as_of: date | None = None, application_id: UUID | None = None) -> Verification: ...


def get_licensing_backend() -> LicensingBackend:
    """The backend intake uses when a caller does not pass one.

    A function rather than a module-level instance, so settings are read at call time and
    a test can substitute a backend without touching import order.
    """
    return LicensingClient()


def verify_and_record(
    conn: psycopg.Connection,
    contractor_id: UUID,
    license_number: str,
    *,
    client: LicensingBackend | None = None,
    application_id: UUID | None = None,
    as_of: date | None = None,
) -> tuple[LicenseStatus, IntakeEffect, str]:
    """Verify a licence and write the result onto the contractor and application (FR-03)."""
    client = client or get_licensing_backend()
    result = client.verification(license_number, as_of=as_of, application_id=application_id)
    status, effect, message = result.status, result.effect, result.message

    # Shaped like the old two-step return so the write below reads the same for either
    # backend. `outcome` carries only what the write needs.
    outcome = CallOutcome(
        verified=result.verified,
        value=(
            LicenseRecord(
                license_number=result.license_number,
                business_name=result.business_name or "",
                license_type="",
                status=result.status,
                issued_on=None,
                expires_on=result.expires_on,
                disciplinary_action_count=0,
            )
            if result.verified
            else None
        ),
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE contractor
            SET license_status = %s,
                license_expires_on = %s,
                last_verified_at = CASE WHEN %s THEN now() ELSE last_verified_at END
            WHERE id = %s
            """,
            (
                status.value,
                outcome.value.expires_on if outcome.value else None,
                outcome.verified,
                str(contractor_id),
            ),
        )
        if application_id is not None:
            cur.execute(
                "UPDATE application SET license_verified = %s WHERE id = %s",
                (outcome.verified, str(application_id)),
            )

    return status, effect, message
