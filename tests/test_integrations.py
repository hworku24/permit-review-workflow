"""External system integrations (FR-02, FR-03, FR-06, NFR-02, NFR-03).

Real SOAP over real HTTP against the mock county service, and real SQL against the
licensing replica. The failure paths matter as much as the happy path: a resilience policy
you cannot demonstrate failing is a resilience policy nobody should believe.
"""

from __future__ import annotations

from datetime import date

import pytest

from permitflow.integrations.base import CircuitBreaker, ResiliencePolicy
from permitflow.integrations.licensing import (
    IntakeEffect,
    LicenseStatus,
    LicensingClient,
    intake_effect,
    verify_and_record,
)
from permitflow.integrations.property_records import PropertyRecordsClient, enrich_parcel

AS_OF = date(2026, 8, 3)


def fast_policy(**overrides) -> ResiliencePolicy:
    """Zero backoff so failure-path tests do not cost wall-clock seconds."""
    defaults = {
        "max_attempts": 3,
        "backoff_base_seconds": 0.0,
        "breaker": CircuitBreaker(failure_threshold=5, reset_seconds=60),
    }
    return ResiliencePolicy(**(defaults | overrides))


@pytest.fixture
def county(soap_service: str, soap_failures) -> PropertyRecordsClient:
    return PropertyRecordsClient(wsdl=soap_service, policy=fast_policy())


class TestPropertyRecords:
    def test_parcel_enrichment_attaches_zoning(self, conn, county, parties) -> None:
        """FR-02."""
        outcome = enrich_parcel(conn, parties["parcel_id"], "14-220-118", client=county)
        assert outcome.verified
        assert outcome.value.zoning_code == "R-90"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT zoning_code, owner_name, last_synced_at FROM parcel WHERE id=%s",
                (str(parties["parcel_id"]),),
            )
            row = cur.fetchone()
        assert row["zoning_code"] == "R-90"
        assert row["owner_name"] == "Jordan Rivas"
        assert row["last_synced_at"] is not None

    def test_stop_work_order_round_trips(self, county) -> None:
        """FR-06 depends on this boolean surviving the SOAP round trip."""
        outcome = county.get_parcel("05-201-330")
        assert outcome.verified and outcome.value.stop_work_order is True

    def test_missing_parcel_is_an_answer_not_an_outage(self, county, soap_failures) -> None:
        outcome = county.get_parcel("99-999-999")
        assert not outcome.verified
        assert outcome.status == "NOT_FOUND"
        assert len(soap_failures.request_log) == 1, "a definitive answer must not be retried"

    def test_transient_failure_is_retried(self, county, soap_failures) -> None:
        """NFR-03: two failures then success."""
        soap_failures.fail_next = 2
        outcome = county.get_parcel("14-220-118")
        assert outcome.verified

    def test_degrades_on_outage(self, conn, county, soap_failures, parties) -> None:
        """NFR-02: a county outage must not stop Rivermont accepting applications."""
        soap_failures.outage = True
        outcome = enrich_parcel(conn, parties["parcel_id"], "14-220-118", client=county)
        assert not outcome.verified
        assert outcome.status == "UNAVAILABLE"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_synced_at FROM parcel WHERE id=%s", (str(parties["parcel_id"]),)
            )
            assert cur.fetchone()["last_synced_at"] is None, "an unverified parcel must look unverified"

    def test_circuit_opens(self, soap_service, soap_failures) -> None:
        """NFR-03: an outage should not cost every applicant the full retry budget."""
        policy = fast_policy(breaker=CircuitBreaker(failure_threshold=3, reset_seconds=60))
        client = PropertyRecordsClient(wsdl=soap_service, policy=policy)

        soap_failures.outage = True
        client.get_parcel("14-220-118")
        assert policy.breaker.is_open

        soap_failures.request_log.clear()
        soap_failures.outage = False
        outcome = client.get_parcel("14-220-118")
        assert not outcome.verified
        assert soap_failures.request_log == [], "an open circuit must not touch the network"

    def test_circuit_closes_after_the_reset_interval(self, soap_service, soap_failures) -> None:
        ticks = {"now": 0.0}
        breaker = CircuitBreaker(
            failure_threshold=2, reset_seconds=60, clock=lambda: ticks["now"]
        )
        client = PropertyRecordsClient(wsdl=soap_service, policy=fast_policy(breaker=breaker))

        soap_failures.outage = True
        client.get_parcel("14-220-118")
        assert breaker.is_open

        ticks["now"] = 61.0
        soap_failures.outage = False
        assert breaker.is_open is False
        assert client.get_parcel("14-220-118").verified

    def test_attempts_are_logged(self, conn, county, soap_failures) -> None:
        """Whose side failed is settled with records, not recollection."""
        soap_failures.fail_next = 1
        county.get_parcel("14-220-118")

        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, attempt FROM integration_call
                   WHERE system='PROPERTY_RECORDS' ORDER BY attempt"""
            )
            rows = cur.fetchall()
        assert [r["status"] for r in rows] == ["FAULT", "SUCCESS"]
        assert [r["attempt"] for r in rows] == [1, 2]


class TestLicensing:
    @pytest.fixture
    def client(self) -> LicensingClient:
        return LicensingClient(policy=fast_policy())

    @pytest.mark.parametrize(
        "license_number,expected_status,expected_effect",
        [
            ("VA-CL-004182", LicenseStatus.ACTIVE, IntakeEffect.PROCEED),
            ("VA-CL-016554", LicenseStatus.ACTIVE, IntakeEffect.PROCEED_WITH_WARNING),
            ("VA-CL-018871", LicenseStatus.EXPIRED, IntakeEffect.RAISE_DEFICIENCY),
            ("VA-CL-020445", LicenseStatus.SUSPENDED, IntakeEffect.ESCALATE_TO_SUPERVISOR),
            ("VA-CL-022108", LicenseStatus.REVOKED, IntakeEffect.ESCALATE_TO_SUPERVISOR),
            ("VA-CL-999999", LicenseStatus.NOT_FOUND, IntakeEffect.RAISE_DEFICIENCY),
        ],
    )
    def test_license_states(
        self, client, license_number, expected_status, expected_effect
    ) -> None:
        """FR-03: every branch of the rules table in docs/04-integration-spec.md."""
        status, effect, _ = intake_effect(client.verify(license_number), as_of=AS_OF)
        assert status == expected_status
        assert effect == expected_effect

    def test_char_padding_is_stripped(self, client) -> None:
        """The replica's license_number is char(12) and comes back padded."""
        outcome = client.verify("VA-CL-004182")
        assert outcome.value.license_number == "VA-CL-004182"
        assert outcome.value.business_name == "Harlowe Building Group LLC"

    def test_revocation_does_not_auto_reject(self, client) -> None:
        """A revocation that is a data error should not deny a permit without a human."""
        _, effect, message = intake_effect(client.verify("VA-CL-022108"), as_of=AS_OF)
        assert effect is IntakeEffect.ESCALATE_TO_SUPERVISOR
        assert "supervisor review" in message

    def test_unreachable_replica_is_never_active(self) -> None:
        """The distinction that matters: could not check is not the same as passed."""
        client = LicensingClient(
            dsn="postgresql://nobody:nobody@127.0.0.1:59999/nope",
            timeout=2,
            policy=fast_policy(),
        )
        outcome = client.verify("VA-CL-004182")
        assert not outcome.verified

        status, effect, _ = intake_effect(outcome, as_of=AS_OF)
        assert status is LicenseStatus.UNVERIFIED
        assert status is not LicenseStatus.ACTIVE
        assert effect is IntakeEffect.RAISE_DEFICIENCY

    def test_expiry_date_beats_a_stale_status_column(self, client) -> None:
        """The replica's status column lags, so the date is trusted over the label."""
        status, effect, _ = intake_effect(
            client.verify("VA-CL-016554"), as_of=date(2027, 1, 1)
        )
        assert status is LicenseStatus.EXPIRED
        assert effect is IntakeEffect.RAISE_DEFICIENCY

    def test_verify_and_record_writes_the_result(self, conn, client, parties) -> None:
        status, _, _ = verify_and_record(
            conn, parties["contractor_id"], "VA-CL-004182", client=client, as_of=AS_OF
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT license_status, last_verified_at FROM contractor WHERE id=%s",
                (str(parties["contractor_id"]),),
            )
            row = cur.fetchone()
        assert row["license_status"] == status == LicenseStatus.ACTIVE
        assert row["last_verified_at"] is not None

    def test_unverified_does_not_stamp_last_verified(self, conn, parties) -> None:
        client = LicensingClient(
            dsn="postgresql://nobody:nobody@127.0.0.1:59999/nope",
            timeout=2,
            policy=fast_policy(),
        )
        verify_and_record(
            conn, parties["contractor_id"], "VA-CL-004182", client=client, as_of=AS_OF
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT license_status, last_verified_at FROM contractor WHERE id=%s",
                (str(parties["contractor_id"]),),
            )
            row = cur.fetchone()
        assert row["license_status"] == "UNVERIFIED"
        assert row["last_verified_at"] is None
