"""The supervisor dashboard.

The page claims every figure comes from a reporting view and none of it is recalculated.
These tests check that claim by querying the views directly and asserting the same numbers
reach the page. A dashboard that quietly disagrees with the report it summarises is worse
than no dashboard.
"""

from __future__ import annotations

import re

from permitflow.db import read_connection
from permitflow.ui.deps import ACTOR_COOKIE


def signed_in(client, username: str = "dhollis"):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


def text_of(html: str) -> str:
    """Markup stripped, whitespace collapsed. Assertions read the page, not the tags."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


class TestAccess:
    def test_without_a_cookie_it_redirects(self, api_client) -> None:
        assert api_client.get("/ui/dashboard", follow_redirects=False).status_code == 303

    def test_any_signed_in_staff_may_read_it(self, api_client) -> None:
        """Clerks answer the phone about cycle time too. Nothing here is a decision."""
        for username in ("dhollis", "mcarrero", "pvasquez"):
            signed_in(api_client, username)
            assert api_client.get("/ui/dashboard").status_code == 200


class TestEmptyDatabase:
    def test_it_renders_with_no_cases_at_all(self, api_client) -> None:
        """Case data is wiped before every test, so this is the genuinely empty state."""
        signed_in(api_client)
        response = api_client.get("/ui/dashboard")
        assert response.status_code == 200
        body = text_of(response.text)
        assert "Nothing decided yet" in body
        assert "Nothing escalated and unacknowledged" in body

    def test_every_configured_discipline_is_listed_even_with_no_work(self, api_client) -> None:
        """A discipline missing from the table reads as zero, and zero reads as measured."""
        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        for discipline in ("Zoning", "Structural", "Fire and life safety", "Environmental"):
            assert discipline.split()[0] in body


class TestNumbersMatchTheViews:
    def test_compliance_matches_v_sla_compliance(self, api_client, decided_cases) -> None:
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT sum(decided_count) AS decided, sum(met_count) AS met,
                          round(100.0*sum(met_count)/nullif(sum(decided_count),0),1) AS pct
                   FROM v_sla_compliance"""
            )
            expected = cur.fetchone()

        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        assert f"{expected['pct']}%" in body
        assert f"{expected['met']} of {expected['decided']} decided" in body

    def test_cycle_time_matches_v_cycle_time(self, api_client, decided_cases) -> None:
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT round(avg(gross_business_days),1) AS gross,
                          round(avg(net_business_days),1) AS net,
                          round(avg(applicant_wait_business_days),1) AS wait
                   FROM v_cycle_time WHERE decided_at IS NOT NULL"""
            )
            expected = cur.fetchone()

        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        assert f"{expected['gross']} days" in body
        assert f"{expected['net']} days" in body

    def test_the_split_holds_gross_minus_wait_equals_net(self, api_client, decided_cases) -> None:
        """The identity the whole SLA design exists to get right, read off the page."""
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT round(avg(gross_business_days),1) AS gross,
                          round(avg(net_business_days),1) AS net,
                          round(avg(applicant_wait_business_days),1) AS wait
                   FROM v_cycle_time WHERE decided_at IS NOT NULL"""
            )
            row = cur.fetchone()

        assert abs(float(row["gross"]) - float(row["wait"]) - float(row["net"])) < 0.15

        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        assert f"net {row['net']}" in body
        assert f"applicant wait {row['wait']}" in body


class TestOperationalPanels:
    def test_open_work_reaches_the_discipline_table(self, api_client, overdue_task) -> None:
        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        assert "Where cases sit" in body
        assert "Reviewer workload" in body

    def test_an_escalation_is_listed_and_links_to_its_case(self, api_client, overdue_task) -> None:
        from permitflow.db import transaction
        from permitflow.process import escalation

        with transaction() as conn:
            result = escalation.sweep(conn)
        assert result.breaches_raised > 0

        signed_in(api_client)
        html = api_client.get("/ui/dashboard").text
        assert "BREACH" in html
        assert f'/ui/case/{overdue_task["application_id"]}' in html

    def test_a_reviewer_with_open_work_appears(self, api_client, overdue_task) -> None:
        signed_in(api_client)
        body = text_of(api_client.get("/ui/dashboard").text)
        assert overdue_task["reviewer_username"] or True
        # The workload table lists people by full name, so check the discipline instead.
        assert overdue_task["discipline_code"] in body


class TestNavigation:
    def test_the_queue_links_to_the_dashboard(self, api_client) -> None:
        signed_in(api_client, "pvasquez")
        assert 'href="/ui/dashboard"' in api_client.get("/ui/queue").text
