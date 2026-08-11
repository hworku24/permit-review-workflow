"""Build the demo database in one command.

    PYTHONPATH=. python scripts/seed_demo.py

Resets, generates a year of history so the reports have something to report on, then adds
five cases parked in the states the walkthrough needs. Running it twice produces the same
database, which is the only reason it is safe to run five minutes before a demo.

The five cases are driven through the engine like everything else, so each one is in its
state because the rules put it there. Parking a case by writing a status column directly
would produce a case detail page whose timeline disagrees with its status.

Parcel APNs for these five start at 90, and the generated history only ever uses 10 to 39,
so the two sets cannot collide.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import seed_cases

from permitflow.ai import recording, triage
from permitflow.config import DEPARTMENT_TZ
from permitflow.db import get_pool, transaction
from permitflow.demo import reset_case_data
from permitflow.integrations.base import ResiliencePolicy
from permitflow.integrations.licensing import LicensingClient, verify_and_record
from permitflow.process import escalation
from permitflow.process.engine import Actor, Engine
from permitflow.process.states import Role

CLERK = Actor("mcarrero", Role.INTAKE_CLERK)
SUPERVISOR = Actor("dhollis", Role.SUPERVISOR)

#: Business days back from the run date for each demo case. The escalated one is far
#: enough back that the sweep finds it past its allowance at real now, which is what lets
#: a breach be raised without writing a due date by hand.
STALE_DAYS_BACK = 125
RECENT_DAYS_BACK = 14

#: Left after the generated history so the last case decides shortly before the run date.
#: Without it the newest history would still be months old and every "last 30 days" column
#: on the dashboard reads zero, which looks like a broken query and not a quiet month.
HISTORY_TAIL_DAYS = 25


def _at(today: date, days_back: int, *, hour: int) -> datetime:
    """A department-local timestamp `days_back` calendar days before the run date."""
    return datetime.combine(today - timedelta(days=days_back), time(hour=hour), DEPARTMENT_TZ)


def _run_triage(conn, application_id: UUID, *, decision: str, actor: str = "mcarrero") -> None:
    """Draft the intake fields and routing, then leave the row in the state asked for.

    Same calls the API route makes. `decision` is "pending", "accepted", or "overridden",
    so the triage screen has one of each to show. A screen that only ever displays the
    happy path does not tell a reviewer what an override looks like.
    """
    app = Engine(conn).get_application(application_id)
    narrative = app["scope_narrative"]

    extraction = triage.extract(narrative)
    routing = triage.recommend_routing(conn, app["permit_type_code"], narrative)

    extraction_id = recording.record(
        conn,
        application_id=application_id,
        kind="field_extraction",
        payload=extraction.as_payload(),
        model=extraction.model,
        prompt_version=extraction.prompt_version,
        confidence=(
            round(sum(s.confidence for s in extraction.accepted.values()) / len(extraction.accepted), 3)
            if extraction.accepted
            else None
        ),
    )
    recording.record(
        conn,
        application_id=application_id,
        kind="discipline_routing",
        payload=routing.as_payload(),
        model=routing.model,
        prompt_version=routing.prompt_version,
    )

    if decision == "accepted":
        recording.decide(conn, extraction_id, actor=actor, accepted=True)
    elif decision == "overridden":
        # The clerk keeps the extraction but corrects one field. Recording the corrected
        # value next to the original is what makes the override reviewable later.
        corrected = extraction.as_payload()
        corrected["clerk_correction"] = {
            "field": "declared_valuation",
            "reason": "figure on the submitted cost affidavit differs from the narrative",
        }
        recording.decide(conn, extraction_id, actor=actor, accepted=False, override_payload=corrected)


def _parties(conn, *, slot: int, name: str, business: str, license_number: str) -> dict:
    """One applicant, parcel, and contractor for a demo case."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applicant (full_name, email) VALUES (%s, %s) RETURNING id",
            (name, f"demo{slot}@example.com"),
        )
        applicant_id = cur.fetchone()["id"]

        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code)
               VALUES (%s, %s, %s) RETURNING id""",
            (f"90-{slot:03d}-{slot:03d}", f"{100 + slot} Copperline Blvd", "R-90"),
        )
        parcel_id = cur.fetchone()["id"]

        # Inserted UNVERIFIED. Whether it becomes ACTIVE is for the licensing lookup to
        # decide, and starting it at ACTIVE would mean the demo asserts a fact it has not
        # checked yet.
        cur.execute(
            """INSERT INTO contractor (license_number, business_name, license_status)
               VALUES (%s, %s, 'UNVERIFIED') RETURNING id""",
            (license_number, business),
        )
        contractor_id = cur.fetchone()["id"]

    return {
        "applicant_id": applicant_id,
        "parcel_id": parcel_id,
        "contractor_id": contractor_id,
        "applicant": Actor(f"demo{slot}", Role.APPLICANT),
    }


def _start(conn, clock, parties: dict, *, permit_type: str, narrative: str, value: int) -> UUID:
    """Create, submit, and enrich. Every demo case passes through here."""
    engine = Engine(conn, clock=clock)
    application_id = engine.create_application(
        applicant_id=parties["applicant_id"],
        parcel_id=parties["parcel_id"],
        contractor_id=parties["contractor_id"],
        permit_type_code=permit_type,
        scope_narrative=narrative,
        declared_valuation=Decimal(value),
        square_feet=640,
        actor=parties["applicant"],
    )
    engine.submit(application_id, parties["applicant"])
    clock.advance_business_days(1, random.Random(1))
    engine.complete_enrichment(application_id)
    return application_id


def _receive_documents(conn, application_id: UUID, clock, username: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE application_document
               SET status='RECEIVED', filename='submitted.pdf', uploaded_at=%s, uploaded_by=%s
               WHERE application_id=%s AND status='MISSING'""",
            (clock.now, username, str(application_id)),
        )


def _number(conn, application_id: UUID) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT application_number FROM application WHERE id=%s", (str(application_id),))
        return cur.fetchone()["application_number"]


def golden_path(conn, rng, today: date) -> tuple[str, str]:
    """Submitted, screened, reviewed clean, issued."""
    clock = seed_cases.Clock(_at(today, RECENT_DAYS_BACK, hour=9))
    parties = _parties(
        conn, slot=1, name="Priya Venkataraman", business="Harlowe Building Group LLC",
        license_number="VA-CL-004182",
    )
    application_id = _start(
        conn, clock, parties,
        permit_type="BLD-RES-ALT",
        narrative=(
            "Rear addition of 640 sq ft to an existing single-family home. New load-bearing "
            "beam at the rear wall and a foundation extension. Declared value $148,000."
        ),
        value=148_000,
    )
    engine = Engine(conn, clock=clock)

    # A real lookup against the replica, so this case carries a successful integration_call
    # row next to the failed ones from the outage case.
    verify_and_record(
        conn, parties["contractor_id"], "VA-CL-004182", application_id=application_id
    )

    _run_triage(conn, application_id, decision="accepted")
    _receive_documents(conn, application_id, clock, parties["applicant"].username)
    clock.advance_business_days(1, rng)
    engine.accept_intake(application_id, CLERK)

    clock.advance_business_days(6, rng)
    for task in seed_cases._open_tasks(conn, application_id):
        actor = seed_cases._reviewer_actor(conn, task["id"])
        engine.start_task(task["id"], actor)
        engine.approve_task(task["id"], actor)

    clock.advance_business_days(1, rng)
    engine.issue(application_id, SUPERVISOR)
    return _number(conn, application_id), "issued, clean review, full audit trail"


def awaiting_review(conn, rng, today: date) -> tuple[str, str]:
    """Intake accepted, four tasks open, nobody has started one."""
    clock = seed_cases.Clock(_at(today, RECENT_DAYS_BACK, hour=10))
    parties = _parties(
        conn, slot=2, name="Marcus Okafor", business="Southgate Commercial Builders",
        license_number="VA-CL-013006",
    )
    application_id = _start(
        conn, clock, parties,
        permit_type="BLD-COM-NEW",
        narrative=(
            "New commercial building of 12,500 sq ft. Site grading, stormwater management, "
            "and full fire suppression. Declared value $2,800,000."
        ),
        value=2_800_000,
    )
    engine = Engine(conn, clock=clock)
    verify_and_record(
        conn, parties["contractor_id"], "VA-CL-013006", application_id=application_id
    )
    _receive_documents(conn, application_id, clock, parties["applicant"].username)
    clock.advance_business_days(1, rng)
    # Environmental is confirmed at screening because the narrative mentions stormwater.
    # Four disciplines open at once, which is the point of the queue screen.
    engine.accept_intake(application_id, CLERK, confirmed_additional_disciplines=["ENVIRONMENTAL"])
    _run_triage(conn, application_id, decision="pending")
    return _number(conn, application_id), "under review, four tasks open, none started"


def paused_on_applicant(conn, rng, today: date) -> tuple[str, str]:
    """Returned incomplete. Sitting with the applicant and the SLA clock stopped."""
    clock = seed_cases.Clock(_at(today, RECENT_DAYS_BACK, hour=11))
    parties = _parties(
        conn, slot=3, name="Dana Whitfield", business="Delmar Renovations Inc",
        license_number="VA-CL-011290",
    )
    application_id = _start(
        conn, clock, parties,
        permit_type="BLD-RES-NEW",
        narrative=(
            "New single family home. Clearing and grading, adds 2,900 sq ft of impervious "
            "area and a stormwater facility. Declared value $612,000."
        ),
        value=612_000,
    )
    engine = Engine(conn, clock=clock)
    clock.advance_business_days(2, rng)
    engine.return_incomplete(
        application_id, CLERK,
        ["site plan not drawn to scale", "survey missing the surveyor's seal"],
    )
    return _number(conn, application_id), "returned incomplete, clock paused on the applicant"


def escalated(conn, rng, today: date) -> tuple[str, str]:
    """Opened months ago and never finished, so the sweep finds it past its allowance."""
    clock = seed_cases.Clock(_at(today, STALE_DAYS_BACK, hour=9))
    parties = _parties(
        conn, slot=4, name="Glen Sutton-Reyes", business="Foxglove Design Build",
        license_number="VA-CL-025637",
    )
    application_id = _start(
        conn, clock, parties,
        permit_type="BLD-COM-NEW",
        narrative=(
            "New commercial building of 18,000 sq ft. Site grading, stormwater management, "
            "and full fire suppression. Declared value $4,100,000."
        ),
        value=4_100_000,
    )
    engine = Engine(conn, clock=clock)
    _receive_documents(conn, application_id, clock, parties["applicant"].username)
    clock.advance_business_days(1, rng)
    engine.accept_intake(application_id, CLERK, confirmed_additional_disciplines=["ENVIRONMENTAL"])

    # One reviewer picks it up and it stalls there. The rest never get started.
    clock.advance_business_days(3, rng)
    tasks = seed_cases._open_tasks(conn, application_id)
    if tasks:
        actor = seed_cases._reviewer_actor(conn, tasks[0]["id"])
        engine.start_task(tasks[0]["id"], actor)
    return _number(conn, application_id), f"open {STALE_DAYS_BACK} days, past its allowance"


def unverified_licence(conn, rng, today: date) -> tuple[str, str]:
    """Intake ran while the licensing replica was unreachable.

    Driven through the real client against a port nothing is listening on, so the retries,
    the failure classification, and the integration_call rows are all genuine. The licence
    is recorded UNVERIFIED and never ACTIVE, which is the rule worth showing.
    """
    clock = seed_cases.Clock(_at(today, RECENT_DAYS_BACK, hour=14))
    parties = _parties(
        conn, slot=5, name="Nadia Malik", business="Pinecrest Construction Co",
        license_number="VA-CL-007733",
    )
    application_id = _start(
        conn, clock, parties,
        permit_type="BLD-RES-ACC",
        narrative="Detached accessory structure of 420 sq ft in the rear yard. Declared value $28,000.",
        value=28_000,
    )

    dead = LicensingClient(
        dsn="postgresql://permitflow_ro:readonly@127.0.0.1:59999/licensing_replica",
        timeout=1,
        policy=ResiliencePolicy.from_settings(max_attempts=2, backoff_base_seconds=0.1),
    )
    status, effect, message = verify_and_record(
        conn,
        parties["contractor_id"],
        "VA-CL-007733",
        client=dead,
        application_id=application_id,
    )
    _run_triage(conn, application_id, decision="overridden")
    return _number(conn, application_id), f"licence {status}, intake effect {effect}"


SCENARIOS = [golden_path, awaiting_review, paused_on_applicant, escalated, unverified_licence]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=120, help="generated history before the demo cases")
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=date.today(),
        help="run date the history is worked backwards from; pin it to reproduce an exact database",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    try:
        with get_pool().connection() as conn:
            conn.autocommit = True
            reset_case_data(conn)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"reset failed ({exc}); is the database up? run: docker compose up -d")
    print("Reset: existing case data cleared.")

    rng = random.Random(args.seed)
    # Worked backwards from the run date so the newest history is days old and not months.
    # Two runs on the same day still produce the same database; --today pins it further.
    span_days = int(args.cases * 2.4) + HISTORY_TAIL_DAYS
    start = _at(args.today, span_days, hour=9)
    for index in range(args.cases):
        clock = seed_cases.Clock(start + timedelta(days=index * 2.4, hours=rng.randint(0, 6)))
        with transaction() as conn:
            summary = seed_cases.seed_one(conn, clock, rng, index)
        if not args.quiet:
            print(f"  {summary}")
    print(f"\nSeeded {args.cases} cases of history, {start.date()} to about {args.today}.")

    print("\nDemo cases:")
    labelled = []
    for scenario in SCENARIOS:
        with transaction() as conn:
            number, note = scenario(conn, rng, args.today)
        labelled.append((number, scenario.__name__, note))
        print(f"  {number}  {scenario.__name__:20} {note}")

    with transaction() as conn:
        result = escalation.sweep(conn)
    print(f"\nEscalation sweep: {result.warnings_raised} warning, {result.breaches_raised} breach")

    print("\nStart here in the demo:")
    print(f"  queue          {labelled[1][0]}")
    print(f"  full history   {labelled[0][0]}")
    print(f"  paused clock   {labelled[2][0]}")
    print(f"  escalation     {labelled[3][0]}")
    print(f"  licence outage {labelled[4][0]}")


if __name__ == "__main__":
    main()
