"""The reporting endpoints.

These had no tests at all, and a missing cast in the compliance filter meant the endpoint
returned 500 whenever the optional permit type was left out. It survived every local run and
failed on the first request to the deployed instance, which is the argument for testing the
unfiltered call and not only the filtered one.
"""

from __future__ import annotations


def hdr(username: str = "dhollis") -> dict[str, str]:
    return {"X-Actor": username}


class TestSlaCompliance:
    def test_without_a_permit_type_filter(self, api_client, decided_cases) -> None:
        """The call that was broken. An untyped NULL parameter has no inferable type."""
        response = api_client.get("/reports/sla-compliance", headers=hdr())
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_with_a_permit_type_filter(self, api_client, decided_cases) -> None:
        response = api_client.get(
            "/reports/sla-compliance", params={"permit_type_code": "BLD-RES-ALT"}, headers=hdr()
        )
        assert response.status_code == 200
        for row in response.json():
            assert row["permit_type_code"] == "BLD-RES-ALT"

    def test_a_filter_that_matches_nothing_is_empty_and_not_an_error(
        self, api_client, decided_cases
    ) -> None:
        response = api_client.get(
            "/reports/sla-compliance", params={"permit_type_code": "NOT-A-TYPE"}, headers=hdr()
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_it_matches_the_view(self, api_client, decided_cases) -> None:
        from permitflow.db import read_connection

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM v_sla_compliance")
            expected = cur.fetchone()["n"]
        assert len(api_client.get("/reports/sla-compliance", headers=hdr()).json()) == expected

    def test_it_needs_an_actor(self, api_client) -> None:
        assert api_client.get("/reports/sla-compliance").status_code == 401


class TestCycleTime:
    def test_it_returns_rows(self, api_client, decided_cases) -> None:
        response = api_client.get("/reports/cycle-time", headers=hdr())
        assert response.status_code == 200
        assert len(response.json()) == len(decided_cases)

    def test_the_limit_is_honoured(self, api_client, decided_cases) -> None:
        response = api_client.get("/reports/cycle-time", params={"limit": 1}, headers=hdr())
        assert len(response.json()) == 1

    def test_gross_minus_wait_equals_net_on_every_row(self, api_client, decided_cases) -> None:
        """The identity, asserted through the API as well as in the view."""
        for row in api_client.get("/reports/cycle-time", headers=hdr()).json():
            assert (
                row["gross_business_days"] - row["applicant_wait_business_days"]
                == row["net_business_days"]
            )
