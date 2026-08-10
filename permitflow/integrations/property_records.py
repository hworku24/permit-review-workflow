"""County property records client (integration I1, SOAP).

A real zeep client against the WSDL the county publishes. The mock in `soap_mock/` speaks
the same contract, so switching to the county's real endpoint is a change to one
environment variable.

FR-02 attaches parcel, zoning, and ownership to the application. FR-06 refuses submission
on a parcel with an open stop-work order. NFR-02 says neither of those may block intake
when the county is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import zeep
from requests import Session
from requests.exceptions import RequestException
from zeep.exceptions import Fault, TransportError
from zeep.transports import Transport

from ..config import get_settings
from .base import CallOutcome, ResiliencePolicy, call_with_resilience

SYSTEM = "PROPERTY_RECORDS"
OPERATION = "GetParcelByAPN"


@dataclass(frozen=True)
class ParcelRecord:
    apn: str
    situs_address: str
    zoning_code: str
    owner_name: str | None
    acreage: Decimal | None
    stop_work_order: bool
    last_assessed_date: date | None


class ParcelNotFound(Exception):
    """The county has no record of this APN. A definitive answer, so not retried."""


def _is_not_found(exc: Exception) -> bool:
    """True when the county gave a definitive "no such parcel".

    Walks the cause chain because `call_with_resilience` wraps the original fault in an
    IntegrationError before re-raising, and the caller still needs to tell a missing
    parcel apart from an unreachable county.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, Fault):
            detail = getattr(current, "detail", None)
            if detail is None:
                return "no parcel on record" in str(current).lower()
            return any("ParcelNotFoundFault" in str(child.tag) for child in detail.iter())
        current = current.__cause__
    return False


def _is_retryable(exc: Exception) -> bool:
    """A missing parcel is an answer. Everything else is worth another try."""
    if _is_not_found(exc):
        return False
    return isinstance(exc, (Fault, TransportError, RequestException, OSError))


def _classify(exc: Exception) -> str:
    if isinstance(exc, Fault):
        return "FAULT"
    if isinstance(exc, (TransportError, RequestException, OSError)):
        return "TIMEOUT" if "timeout" in str(exc).lower() else "ERROR"
    return "ERROR"


class PropertyRecordsClient:
    def __init__(
        self,
        wsdl: str | None = None,
        timeout: float | None = None,
        policy: ResiliencePolicy | None = None,
    ) -> None:
        settings = get_settings()
        self.wsdl = wsdl or settings.property_records_wsdl
        self.timeout = timeout or settings.property_records_timeout_seconds
        self.policy = policy or ResiliencePolicy.from_settings()
        self._client: zeep.Client | None = None

    @property
    def client(self) -> zeep.Client:
        """Built on first use.

        Constructing a zeep client fetches and parses the WSDL, which is a network call.
        Doing that at import time would make the whole application fail to start whenever
        the county is having a bad morning.
        """
        if self._client is None:
            session = Session()
            transport = Transport(session=session, timeout=self.timeout, operation_timeout=self.timeout)
            self._client = zeep.Client(wsdl=self.wsdl, transport=transport)
        return self._client

    def get_parcel(self, apn: str, application_id: UUID | None = None) -> CallOutcome[ParcelRecord]:
        """Look up a parcel. Never raises for a service problem.

        Returns an outcome carrying `verified=False` when the county could not answer, so
        the caller can accept the application and show the clerk that the parcel is
        unverified (NFR-02).
        """

        def _call() -> Any:
            return self.client.service.GetParcelByAPN(APN=apn)

        try:
            raw = call_with_resilience(
                system=SYSTEM,
                operation=OPERATION,
                fn=_call,
                policy=self.policy,
                is_retryable=_is_retryable,
                classify=_classify,
                application_id=application_id,
                request_payload={"APN": apn},
                response_summary=lambda r: {"ZoningCode": str(getattr(r, "ZoningCode", None))},
            )
        except Exception as exc:  # noqa: BLE001 - degrade, per NFR-02
            status = "NOT_FOUND" if _is_not_found(exc) else "UNAVAILABLE"
            return CallOutcome(verified=False, status=status, detail=str(exc)[:300])

        return CallOutcome(
            verified=True,
            value=ParcelRecord(
                apn=str(raw.APN),
                situs_address=str(raw.SitusAddress),
                zoning_code=str(raw.ZoningCode),
                owner_name=str(raw.OwnerName) if raw.OwnerName is not None else None,
                acreage=Decimal(str(raw.Acreage)) if raw.Acreage is not None else None,
                stop_work_order=bool(raw.StopWorkOrder),
                last_assessed_date=raw.LastAssessedDate,
            ),
        )


def enrich_parcel(
    conn: psycopg.Connection,
    parcel_id: UUID,
    apn: str,
    *,
    client: PropertyRecordsClient | None = None,
    application_id: UUID | None = None,
) -> CallOutcome[ParcelRecord]:
    """Refresh a parcel row from the county and report whether it worked (FR-02).

    `last_synced_at` is only stamped on a successful lookup. A parcel that has never been
    verified keeps a null there, which is what makes the gap visible on the intake queue
    rather than invisible behind stale-looking but plausible data.
    """
    client = client or PropertyRecordsClient()
    outcome = client.get_parcel(apn, application_id=application_id)

    if outcome.verified and outcome.value is not None:
        record = outcome.value
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE parcel
                SET situs_address = %s,
                    zoning_code = %s,
                    owner_name = %s,
                    acreage = %s,
                    stop_work_order = %s,
                    last_synced_at = now()
                WHERE id = %s
                """,
                (
                    record.situs_address,
                    record.zoning_code,
                    record.owner_name,
                    record.acreage,
                    record.stop_work_order,
                    str(parcel_id),
                ),
            )

    if application_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE application SET parcel_verified = %s WHERE id = %s",
                (outcome.verified, str(application_id)),
            )

    return outcome
