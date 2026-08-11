"""The two licensing backends have to agree.

The verification rules exist twice: once in Python in `permitflow/integrations/licensing.py`
and once in Java in `licensing-verifier/`. Two copies of one rule is a real risk of the same
kind as the business day arithmetic, and the failure is quiet. A status precedence that
drifts in one implementation produces a licence decision that depends on which backend
happened to answer, and nobody notices until an appeal.

So both are driven over the same replica rows and compared field by field. The rows are
chosen to cover the cases where an implementation is most likely to disagree: an expiry
boundary, a status column that lags the expiry date, a licence that does not exist, and the
`char(12)` padding the replica stores.

Skipped when the Java service is not running, because a fresh clone has no JVM obligation
(NFR-06). CI runs it, since CI starts the service.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from permitflow.config import get_settings
from permitflow.integrations.licensing import (
    HttpLicensingBackend,
    LicensingClient,
    get_licensing_backend,
)

SERVICE = "http://localhost:8082"

#: Seeded in sql/legacy/001_licensing.sql, chosen for the disagreements they would expose.
CASES = [
    ("VA-CL-004182", "active, expires well in the future"),
    ("VA-CL-018871", "expired, and the status column says so"),
    ("VA-CL-020445", "suspended"),
    ("VA-CL-022108", "revoked"),
    ("VA-CL-016554", "active but inside the expiry warning window"),
    ("VA-CL-999999", "no such licence"),
    ("  VA-CL-004182  ", "padded, as the char(12) column stores it"),
]


def service_running() -> bool:
    try:
        httpx.get(f"{SERVICE}/internal/licenses/VA-CL-004182/verification", timeout=2)
    except httpx.HTTPError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not service_running(),
    reason="licensing-verifier is not running; start it with ./mvnw spring-boot:run",
)


@pytest.mark.parametrize("license_number,description", CASES, ids=[c[1] for c in CASES])
def test_both_backends_return_the_same_answer(license_number: str, description: str) -> None:
    as_of = date(2026, 8, 11)
    direct = LicensingClient().verification(license_number, as_of=as_of)
    served = HttpLicensingBackend(base_url=SERVICE).verification(license_number, as_of=as_of)

    assert served.verified == direct.verified, description
    assert served.status == direct.status, description
    assert served.effect == direct.effect, description
    assert served.expires_on == direct.expires_on, description
    # Business name is the one field the replica pads, so this is the padding check.
    assert (served.business_name or "").strip() == (direct.business_name or "").strip()


def test_the_expiry_boundary_is_the_same_day_in_both() -> None:
    """A licence expiring on a date is active up to it and expired after.

    An off-by-one here would show up as one implementation issuing a permit the other would
    have flagged, on exactly one day of the year.
    """
    number = "VA-CL-016554"  # expires 2026-08-28
    for as_of in (date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 29)):
        direct = LicensingClient().verification(number, as_of=as_of)
        served = HttpLicensingBackend(base_url=SERVICE).verification(number, as_of=as_of)
        assert served.status == direct.status, f"disagreed on {as_of}"
        assert served.effect == direct.effect, f"disagreed on {as_of}"


def test_an_unreachable_service_is_unverified_and_not_a_pass() -> None:
    """The whole point of the seam surviving a failure.

    A service that cannot be reached is not a licence that passed, and it is not a licence
    that failed either. It is an answer nobody has.
    """
    from permitflow.integrations.base import ResiliencePolicy

    dead = HttpLicensingBackend(
        base_url="http://127.0.0.1:59998",
        timeout=1,
        policy=ResiliencePolicy.from_settings(max_attempts=1, backoff_base_seconds=0),
    )
    result = dead.verification("VA-CL-004182")
    assert result.verified is False
    assert result.status == "UNVERIFIED"
    assert "unreachable" in result.message


def test_the_backend_is_chosen_by_configuration(monkeypatch) -> None:
    """Switching backends is a setting, not a code change."""
    monkeypatch.setenv("LICENSING_BACKEND", "http")
    get_settings.cache_clear()
    assert get_licensing_backend().name == "http"

    monkeypatch.setenv("LICENSING_BACKEND", "direct")
    get_settings.cache_clear()
    assert get_licensing_backend().name == "direct"
    get_settings.cache_clear()
