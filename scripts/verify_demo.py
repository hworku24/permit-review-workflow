"""Check that the demo database can actually back a walkthrough.

    PYTHONPATH=. python scripts/verify_demo.py

Run it after seeding and before demoing. Every check names the screen it protects, because
the failure this guards against is finding out mid-demo that a panel is empty. Exits
non-zero if anything fails, so it can gate a deploy.

The arithmetic checks are the ones worth keeping. Gross minus applicant wait equals net is
the identity the whole SLA design exists to get right, and it is checked on every decided
row here, not on the averages. An average can hold while individual rows are wrong.
"""

from __future__ import annotations

import sys
from typing import Any

from permitflow.db import read_connection

PASS = "ok  "
FAIL = "FAIL"

failures: list[str] = []


def check(screen: str, description: str, condition: bool, detail: str = "") -> None:
    marker = PASS if condition else FAIL
    print(f"  [{marker}] {screen:22} {description}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(f"{screen}: {description} {detail}".strip())


def one(conn, sql: str, params: tuple = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return next(iter(row.values())) if row else None


def rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main() -> None:
    try:
        with read_connection() as conn:
            run_checks(conn)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"could not read the database ({exc}); run: docker compose up -d")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for line in failures:
            print(f"  - {line}")
        sys.exit(1)
    print("Demo database is ready.")


def run_checks(conn) -> None:
    print("Verifying demo data\n")

    # Reviewer queue -----------------------------------------------------------------
    with_open = rows(
        conn,
        "SELECT username, discipline_code, open_tasks FROM v_reviewer_workload WHERE open_tasks > 0",
    )
    check("reviewer queue", "at least two reviewers have open work", len(with_open) >= 2,
          f"{len(with_open)} with open tasks")

    unassigned = one(
        conn,
        "SELECT count(*) AS n FROM review_task WHERE status IN ('ASSIGNED','IN_PROGRESS') AND reviewer_id IS NULL",
    )
    check("reviewer queue", "no started task is missing an assignee", unassigned == 0,
          f"{unassigned} unassigned")

    overdue = one(conn, "SELECT count(*) AS n FROM v_task_sla_status WHERE sla_state = 'BREACHED'")
    check("reviewer queue", "some work is overdue, so the flag is visible", overdue > 0,
          f"{overdue} overdue")

    # Case detail --------------------------------------------------------------------
    thin = rows(
        conn,
        """SELECT a.application_number, count(sh.id) AS n
           FROM application a LEFT JOIN status_history sh ON sh.application_id = a.id
           GROUP BY 1 HAVING count(sh.id) < 2 ORDER BY 1 LIMIT 5""",
    )
    check("case detail", "every case has a status history", not thin,
          f"{len(thin)} with fewer than 2 rows")

    audit_rows = one(conn, "SELECT count(*) AS n FROM audit_log")
    check("case detail", "audit log has entries", audit_rows > 100, f"{audit_rows} rows")

    documents = one(conn, "SELECT count(*) AS n FROM application_document WHERE status='RECEIVED'")
    check("case detail", "documents have been received", documents > 0, f"{documents} received")

    rounds = one(conn, "SELECT max(round) AS n FROM review_task")
    check("case detail", "at least one case reached round 2", (rounds or 0) >= 2,
          f"max round {rounds}")

    # Triage screen ------------------------------------------------------------------
    states = {
        r["state"]: r["n"]
        for r in rows(
            conn,
            """SELECT CASE WHEN accepted IS NULL THEN 'pending'
                           WHEN accepted THEN 'accepted' ELSE 'overridden' END AS state,
                      count(*) AS n
               FROM ai_recommendation WHERE kind = 'field_extraction' GROUP BY 1""",
        )
    }
    for state in ("pending", "accepted", "overridden"):
        check("triage", f"has a {state} extraction", states.get(state, 0) > 0,
              f"{states.get(state, 0)} rows")

    routing = one(conn, "SELECT count(*) AS n FROM ai_recommendation WHERE kind='discipline_routing'")
    check("triage", "routing recommendations exist", routing > 0, f"{routing} rows")

    unattached = one(
        conn,
        "SELECT count(*) AS n FROM ai_recommendation WHERE accepted IS NOT NULL AND decided_by IS NULL",
    )
    check("triage", "every decision names who made it", unattached == 0, f"{unattached} anonymous")

    # Dashboard ----------------------------------------------------------------------
    months = one(conn, "SELECT count(DISTINCT decided_month) AS n FROM v_sla_compliance")
    check("dashboard", "compliance covers several months", months >= 6, f"{months} months")

    recent = one(
        conn,
        "SELECT count(*) AS n FROM v_cycle_time WHERE decided_at > now() - interval '30 days'",
    )
    check("dashboard", "cases were decided in the last 30 days", recent > 0, f"{recent} decided")

    active = one(conn, "SELECT count(*) AS n FROM v_reviewer_workload WHERE completed_last_30_days > 0")
    check("dashboard", "recent completions are attributed", active > 0, f"{active} reviewers")

    escalations = one(conn, "SELECT count(*) AS n FROM v_open_escalations")
    check("dashboard", "open escalations exist", escalations > 0, f"{escalations} open")

    compliance = one(
        conn,
        "SELECT round(100.0*sum(met_count)/nullif(sum(decided_count),0),1) AS pct FROM v_sla_compliance",
    )
    check("dashboard", "compliance is neither 0 nor 100", 0 < float(compliance or 0) < 100,
          f"{compliance}%")

    # SLA and cycle time -------------------------------------------------------------
    decided = one(conn, "SELECT count(*) AS n FROM v_cycle_time WHERE decided_at IS NOT NULL")
    broken = one(
        conn,
        """SELECT count(*) AS n FROM v_cycle_time
           WHERE decided_at IS NOT NULL
             AND gross_business_days - applicant_wait_business_days <> net_business_days""",
    )
    check("sla", "gross minus applicant wait equals net on every decided case", broken == 0,
          f"{decided} checked, {broken} wrong")

    negative = one(
        conn,
        """SELECT count(*) AS n FROM v_cycle_time
           WHERE net_business_days < 0 OR applicant_wait_business_days < 0 OR gross_business_days < 0""",
    )
    check("sla", "no negative durations", negative == 0, f"{negative} negative")

    waited = one(conn, "SELECT count(*) AS n FROM v_cycle_time WHERE applicant_wait_business_days > 0")
    check("sla", "some cases waited on the applicant", waited > 0, f"{waited} with wait")

    open_pause = one(conn, "SELECT count(*) AS n FROM clock_pause WHERE resumed_at IS NULL")
    check("sla", "a case is sitting with the clock paused", open_pause > 0, f"{open_pause} paused")

    # Integrations -------------------------------------------------------------------
    outcomes = {
        r["status"]: r["n"]
        for r in rows(conn, "SELECT status, count(*) AS n FROM integration_call GROUP BY 1")
    }
    check("integrations", "a successful call is logged", outcomes.get("SUCCESS", 0) > 0,
          f"{outcomes.get('SUCCESS', 0)} success")
    check("integrations", "a failed call is logged", outcomes.get("ERROR", 0) > 0,
          f"{outcomes.get('ERROR', 0)} error")

    unverified = one(conn, "SELECT count(*) AS n FROM contractor WHERE license_status='UNVERIFIED'")
    check("integrations", "an unverified licence exists", unverified > 0, f"{unverified} unverified")

    wrongly_active = one(
        conn,
        """SELECT count(*) AS n FROM application a JOIN contractor c ON c.id = a.contractor_id
           WHERE a.license_verified IS FALSE AND c.license_status = 'ACTIVE'""",
    )
    check("integrations", "nothing unverified is recorded ACTIVE", wrongly_active == 0,
          f"{wrongly_active} wrong")


if __name__ == "__main__":
    main()
