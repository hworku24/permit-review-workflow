"""Generate a year of permit history.

Runs the real engine with an injected clock, not raw row writes. That is the
whole point: the resulting history obeys the same guards, writes the same audit trail, and
produces the same clock pauses as production traffic, so the reporting views can be
demonstrated on data that is actually consistent with the rules.

Backdating timestamps after the fact would be faster and would produce a compliance number
nobody should trust.

    python scripts/seed_cases.py --cases 120 --reset

Reference data must already be loaded (sql/003_seed.sql).

`--seed` is fixed by default so a run reproduces exactly, which means a second run against
a database that already holds cases generates the same parcel APNs and violates
`parcel_apn_key`. Seeding therefore refuses to start on a non-empty database unless
`--reset` is passed. Failing with a sentence beats failing with a constraint violation
sixty cases in.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from permitflow.config import DEPARTMENT_TZ
from permitflow.db import get_pool, transaction
from permitflow.demo import case_row_count, reset_case_data
from permitflow.process import escalation
from permitflow.process.engine import Actor, Engine
from permitflow.process.states import Role

CLERKS = [Actor("mcarrero", Role.INTAKE_CLERK), Actor("jtakeda", Role.INTAKE_CLERK)]
SUPERVISOR = Actor("dhollis", Role.SUPERVISOR)

STREETS = [
    "Aldergate Ln", "Fenwick Row", "Copperline Blvd", "Millrace Ct", "Tanner St",
    "Ironwood Way", "Quarry Lane", "Bellhaven Rd", "Sowerby Ave", "Larkfield Dr",
]
FIRST_NAMES = [
    "Jordan", "Priya", "Marcus", "Angela", "Dana", "Tomas", "Nadia", "Glen", "Ruth",
    "Paulo", "Cara", "Andre", "Hana", "Milo", "Sofia", "Desmond",
]
LAST_NAMES = [
    "Rivas", "Venkataraman", "Deel", "Sutton-Reyes", "Whitfield", "Brandt", "Malik",
    "Ferris", "Okafor", "Vasquez", "Lindgren", "Bassett", "Nakamura", "Oyelaran",
]

SCENARIOS = [
    (
        "BLD-RES-ALT",
        "Rear addition of {sf} sq ft to an existing single-family home. New load-bearing "
        "beam at the rear wall and a foundation extension. Declared value ${value:,}.",
        (30_000, 220_000),
        (200, 900),
    ),
    (
        "BLD-RES-NEW",
        "New single family home. Clearing and grading, adds {sf} sq ft of impervious area "
        "and a stormwater facility. Declared value ${value:,}.",
        (350_000, 950_000),
        (1_800, 4_200),
    ),
    (
        "BLD-RES-ACC",
        "Detached accessory structure of {sf} sq ft in the rear yard. Declared value ${value:,}.",
        (8_000, 45_000),
        (120, 480),
    ),
    (
        "BLD-COM-ALT",
        "Tenant fit-out of a {sf} square foot suite. New commercial kitchen with a "
        "suppression hood, revised egress, and sprinkler relocation. Value ${value:,}.",
        (90_000, 600_000),
        (1_200, 6_000),
    ),
    (
        "BLD-COM-NEW",
        "New commercial building of {sf} sq ft. Site grading, stormwater management, "
        "and full fire suppression. Declared value ${value:,}.",
        (900_000, 6_500_000),
        (5_000, 40_000),
    ),
]

DEFICIENCIES = [
    ("IRC R502.3.1", "Floor joist span exceeds the allowable table value", "MAJOR"),
    ("IBC 1808.3", "Footing depth not shown on the building section", "MAJOR"),
    ("§ 59-2.2.4", "Rear setback shown at 24 feet, district minimum is 30", "MAJOR"),
    ("§ 59-7.2.3", "Underground detention alone does not satisfy water quality", "MAJOR"),
    ("IBC 1017.2", "Exit access travel distance exceeds the permitted maximum", "MAJOR"),
    ("§ 59-6.3.1", "Tree canopy calculation omits the removed specimen tree", "MINOR"),
    ("IECC R402.1", "Envelope U-factor table missing for the addition", "MINOR"),
]


class Clock:
    """A clock the seeder advances deliberately."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance_business_days(self, days: int, rng: random.Random) -> None:
        """Move forward roughly `days` business days, jittered so cases do not align."""
        remaining = days
        while remaining > 0:
            self.now += timedelta(days=1)
            if self.now.isoweekday() < 6:
                remaining -= 1
        self.now += timedelta(hours=rng.randint(0, 7))


def _make_parties(conn, rng: random.Random, index: int) -> dict[str, UUID]:
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applicant (full_name, email) VALUES (%s, %s) RETURNING id",
            (name, f"applicant{index}@example.com"),
        )
        applicant_id = cur.fetchone()["id"]

        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code)
               VALUES (%s, %s, %s) RETURNING id""",
            (
                f"{rng.randint(10, 39)}-{rng.randint(100, 499):03d}-{index:03d}",
                f"{rng.randint(1, 3999)} {rng.choice(STREETS)}",
                rng.choice(["R-60", "R-90", "R-200", "RT-8", "C-1", "C-2", "MXD"]),
            ),
        )
        parcel_id = cur.fetchone()["id"]

        license_number = f"VA-CL-{rng.randint(30000, 99999):06d}"
        cur.execute(
            """INSERT INTO contractor (license_number, business_name, license_status)
               VALUES (%s, %s, 'ACTIVE') RETURNING id""",
            (license_number, f"{rng.choice(LAST_NAMES)} Construction LLC"),
        )
        contractor_id = cur.fetchone()["id"]

    return {"applicant_id": applicant_id, "parcel_id": parcel_id, "contractor_id": contractor_id}


def _reviewer_actor(conn, task_id: UUID) -> Actor:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT rt.reviewer_id, r.username FROM review_task rt
               JOIN reviewer r ON r.id = rt.reviewer_id WHERE rt.id = %s""",
            (str(task_id),),
        )
        row = cur.fetchone()
    return Actor(row["username"], Role.REVIEWER, row["reviewer_id"])


def _open_tasks(conn, application_id: UUID) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, discipline_code FROM review_task
               WHERE application_id=%s AND status IN ('PENDING','ASSIGNED','IN_PROGRESS')
               ORDER BY discipline_code""",
            (str(application_id),),
        )
        return cur.fetchall()


def seed_one(conn, clock: Clock, rng: random.Random, index: int) -> str:
    """Drive one application from creation to a decision."""
    permit_type, template, value_range, sf_range = rng.choice(SCENARIOS)
    parties = _make_parties(conn, rng, index)

    valuation = rng.randrange(*value_range, 1000)
    square_feet = rng.randrange(*sf_range, 10)
    narrative = template.format(sf=f"{square_feet:,}", value=valuation)

    engine = Engine(conn, clock=clock)
    applicant = Actor(f"applicant{index}", Role.APPLICANT)
    clerk = rng.choice(CLERKS)

    application_id = engine.create_application(
        applicant_id=parties["applicant_id"],
        parcel_id=parties["parcel_id"],
        contractor_id=parties["contractor_id"],
        permit_type_code=permit_type,
        scope_narrative=narrative,
        declared_valuation=Decimal(valuation),
        square_feet=square_feet,
        actor=applicant,
    )

    engine.submit(application_id, applicant)
    clock.advance_business_days(1, rng)
    engine.complete_enrichment(application_id)

    # About a third of submissions come back incomplete, matching the 31% in discovery.
    if rng.random() < 0.31:
        clock.advance_business_days(rng.randint(1, 3), rng)
        engine.return_incomplete(
            application_id, clerk, rng.sample([
                "site plan not drawn to scale",
                "contractor affidavit unsigned",
                "survey missing the surveyor's seal",
                "energy compliance form incomplete",
            ], k=rng.randint(1, 2)),
        )
        clock.advance_business_days(rng.randint(3, 18), rng)  # applicant time, clock paused
        engine.resubmit(application_id, applicant)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE application_document
               SET status='RECEIVED', filename='submitted.pdf', uploaded_at=%s, uploaded_by=%s
               WHERE application_id=%s AND status='MISSING'""",
            (clock.now, applicant.username, str(application_id)),
        )

    clock.advance_business_days(rng.randint(1, 3), rng)
    additions = ["ENVIRONMENTAL"] if "stormwater" in narrative else []
    engine.accept_intake(application_id, clerk, confirmed_additional_disciplines=additions)

    # Round one.
    clock.advance_business_days(rng.randint(4, 16), rng)
    deficient = False
    for task in _open_tasks(conn, application_id):
        actor = _reviewer_actor(conn, task["id"])
        engine.start_task(task["id"], actor)
        roll = rng.random()
        if roll < 0.28:
            deficient = True
            engine.record_deficiencies(
                task["id"],
                actor,
                [
                    {"code_reference": code, "description": text, "severity": severity}
                    for code, text, severity in rng.sample(DEFICIENCIES, k=rng.randint(1, 3))
                ],
            )
        elif roll < 0.40:
            engine.approve_task_with_conditions(
                task["id"], actor, ["Provide the sealed detail before the first inspection"]
            )
        else:
            engine.approve_task(task["id"], actor)

    # Round two, if anyone found something.
    if deficient:
        clock.advance_business_days(rng.randint(4, 25), rng)  # applicant time, clock paused
        engine.resubmit(application_id, applicant)
        clock.advance_business_days(rng.randint(3, 12), rng)
        for task in _open_tasks(conn, application_id):
            actor = _reviewer_actor(conn, task["id"])
            engine.start_task(task["id"], actor)
            engine.approve_task(task["id"], actor)

    clock.advance_business_days(rng.randint(1, 4), rng)
    if rng.random() < 0.06:
        engine.deny(
            application_id,
            SUPERVISOR,
            "Unresolved deficiency: " + rng.choice(DEFICIENCIES)[1],
        )
        outcome = "DENIED"
    else:
        engine.issue(application_id, SUPERVISOR)
        outcome = "ISSUED"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT application_number FROM application WHERE id=%s", (str(application_id),)
        )
        number = cur.fetchone()["application_number"]
    return f"{number} {permit_type} {outcome}"


def prepare_database(*, reset: bool) -> None:
    """Reset if asked, and refuse to seed on top of existing cases if not."""
    with get_pool().connection() as conn:
        conn.autocommit = True
        if reset:
            reset_case_data(conn)
            print("Reset: existing case data cleared.")
            return
        existing = case_row_count(conn)
    if existing:
        sys.exit(
            f"Database already holds {existing} applications. Seeding is deterministic, so "
            "this run would generate the same parcel APNs and fail partway through.\n"
            "Re-run with --reset to clear case data first."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260803, help="deterministic by default")
    parser.add_argument(
        "--start",
        default="2025-09-01",
        help="first submission date; cases are spread forward from here",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe existing case data first; required to seed a non-empty database",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    prepare_database(reset=args.reset)

    rng = random.Random(args.seed)
    start = datetime.fromisoformat(args.start).replace(hour=9, tzinfo=DEPARTMENT_TZ)

    for index in range(args.cases):
        # Each case gets its own clock starting a little after the last, so submissions
        # are spread across the year and do not all arrive in one burst.
        clock = Clock(start + timedelta(days=index * 2.4, hours=rng.randint(0, 6)))
        with transaction() as conn:
            summary = seed_one(conn, clock, rng, index)
        if not args.quiet:
            print(f"  {summary}")

    with transaction() as conn:
        result = escalation.sweep(conn)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT decided_month, permit_type_code, decided_count, compliance_pct
                   FROM v_sla_compliance ORDER BY decided_month, permit_type_code"""
            )
            compliance = cur.fetchall()

    print(f"\nSeeded {args.cases} cases.")
    print(f"Escalations raised: {result.warnings_raised} warning, {result.breaches_raised} breach")
    print("\nSLA compliance by month (FR-23):")
    for row in compliance:
        print(
            f"  {row['decided_month']}  {row['permit_type_code']:12} "
            f"n={row['decided_count']:3}  {row['compliance_pct']}%"
        )


if __name__ == "__main__":
    main()
