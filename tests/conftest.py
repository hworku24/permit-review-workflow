"""Shared fixtures.

Tests run against a real Postgres, not a fake. The business day functions, the append-only
audit triggers, the partial unique indexes, and the reporting views are all database
behaviour, and testing them against an in-memory substitute would test the substitute.

The SOAP mock runs in a thread on a free port so the integration tests make real HTTP
round trips through a real zeep client.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
import uvicorn

from permitflow.db import get_pool, transaction
from permitflow.process.engine import Actor, Engine
from permitflow.process.sla import HolidayCalendar
from permitflow.process.states import Role

# Case data is wiped between tests. Reference data (permit types, disciplines, staff,
# holidays, SLA policies) is seeded once by docker-entrypoint and left alone.
CASE_TABLES = [
    "audit_log",
    "integration_call",
    "ai_recommendation",
    "escalation",
    "clock_pause",
    "status_history",
    "deficiency",
    "review_condition",
    "review_task",
    "application_document",
    "application",
    "applicant",
    "contractor",
    "parcel",
]

CLERK = Actor("mcarrero", Role.INTAKE_CLERK)
SUPERVISOR = Actor("dhollis", Role.SUPERVISOR)
APPLICANT = Actor("test-applicant", Role.APPLICANT)


@pytest.fixture(scope="session", autouse=True)
def _require_database() -> None:
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM permit_type")
            if cur.fetchone()["n"] == 0:
                pytest.exit("permitflow database has no reference data; run sql/003_seed.sql")
    except Exception as exc:  # noqa: BLE001
        pytest.exit(f"permitflow database unavailable ({exc}); run: docker compose up -d")


@pytest.fixture(autouse=True)
def clean_case_data() -> Iterator[None]:
    """Wipe case rows before each test.

    Note what this has to do to clear `audit_log`. The append-only triggers reject DELETE
    and TRUNCATE, so the harness disables them explicitly as the table owner. That is the
    point rather than a workaround: resetting the audit trail takes a privilege the
    application role never exercises, which is what FR-21 actually asks for.
    """
    with get_pool().connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
            cur.execute(
                f"TRUNCATE {', '.join(CASE_TABLES)} RESTART IDENTITY CASCADE"
            )
            cur.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
            # Reference data is not truncated, but tests are allowed to mutate it (an
            # assignment test deactivates every reviewer to prove a task stays PENDING).
            # Restoring it here is what keeps the suite order-independent.
            cur.execute("UPDATE reviewer SET active = (username <> 'tbrandt')")
    yield


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with transaction() as connection:
        yield connection


@pytest.fixture
def engine(conn: psycopg.Connection) -> Engine:
    return Engine(conn)


@pytest.fixture
def calendar(conn: psycopg.Connection) -> HolidayCalendar:
    from permitflow.db import load_holiday_calendar

    return load_holiday_calendar(conn)


class FrozenClock:
    """A clock the test moves by hand.

    SLA behaviour is defined in business days. Without this, verifying that a task
    escalates at fourteen days would take fourteen days.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: int = 0, hours: int = 0) -> None:
        self.now += timedelta(days=days, hours=hours)


@pytest.fixture
def clock() -> FrozenClock:
    # A Monday, well clear of the seeded holidays.
    return FrozenClock(datetime(2026, 8, 3, 9, 0, tzinfo=UTC))


@pytest.fixture
def parties(conn: psycopg.Connection) -> dict[str, UUID]:
    """An applicant, a contractor with an active licence, and a clean parcel."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applicant (full_name, email) VALUES ('Test Applicant','t@example.com') RETURNING id"
        )
        applicant_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO contractor (license_number, business_name)
               VALUES ('VA-CL-004182','Harlowe Building Group LLC') RETURNING id"""
        )
        contractor_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code)
               VALUES ('14-220-118','1408 Aldergate Ln','R-90') RETURNING id"""
        )
        parcel_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code, stop_work_order)
               VALUES ('05-201-330','9 Quarry Lane','R-60', true) RETURNING id"""
        )
        blocked_parcel_id = cur.fetchone()["id"]
    return {
        "applicant_id": applicant_id,
        "contractor_id": contractor_id,
        "parcel_id": parcel_id,
        "blocked_parcel_id": blocked_parcel_id,
    }


@pytest.fixture
def make_application(conn: psycopg.Connection, parties: dict[str, UUID], clock: FrozenClock):
    """Build an application in whatever state the test needs."""

    def _make(
        permit_type_code: str = "BLD-RES-ALT",
        narrative: str = "Rear addition with a new load-bearing beam, 640 sq ft. Value $180,000.",
        valuation: Decimal | float = Decimal("180000"),
        parcel_key: str = "parcel_id",
        with_contractor: bool = True,
    ) -> UUID:
        engine = Engine(conn, clock=clock)
        return engine.create_application(
            applicant_id=parties["applicant_id"],
            parcel_id=parties[parcel_key],
            contractor_id=parties["contractor_id"] if with_contractor else None,
            permit_type_code=permit_type_code,
            scope_narrative=narrative,
            declared_valuation=valuation,
            actor=APPLICANT,
        )

    return _make


@pytest.fixture
def under_review(conn: psycopg.Connection, make_application, clock: FrozenClock):
    """An application advanced to UNDER_REVIEW with all documents satisfied.

    Returns the application id and a helper for looking up its current task per discipline,
    which most review tests need.
    """

    def _setup(permit_type_code: str = "BLD-RES-ALT", **kwargs) -> tuple[UUID, callable]:
        engine = Engine(conn, clock=clock)
        application_id = make_application(permit_type_code=permit_type_code, **kwargs)
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document
                   SET status='RECEIVED', filename='x.pdf', uploaded_at=now(), uploaded_by='t'
                   WHERE application_id=%s AND status='MISSING'""",
                (str(application_id),),
            )
        engine.accept_intake(application_id, CLERK)

        def current_task(discipline: str) -> dict:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT rt.*, r.username FROM review_task rt
                       LEFT JOIN reviewer r ON r.id = rt.reviewer_id
                       WHERE rt.application_id=%s AND rt.discipline_code=%s
                       ORDER BY rt.round DESC LIMIT 1""",
                    (str(application_id), discipline),
                )
                return cur.fetchone()

        return application_id, current_task

    return _setup


def reviewer_actor(task: dict) -> Actor:
    """The Actor for whoever a task is assigned to."""
    return Actor(task["username"], Role.REVIEWER, task["reviewer_id"])


# ---------------------------------------------------------------------------
# Mock county SOAP service
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def soap_service() -> Iterator[str]:
    """Run the mock property records service and yield its WSDL URL."""
    from permitflow.integrations.soap_mock.server import app as soap_app

    port = _free_port()
    config = uvicorn.Config(soap_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover
        pytest.fail("mock SOAP service did not start")

    yield f"http://127.0.0.1:{port}/property-records?wsdl"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def api_client(soap_service: str, monkeypatch):
    """A TestClient wired to the in-process mock county service.

    Settings are cached, so the env var is set and the cache cleared on both sides. The
    database pool is unaffected because it is keyed on a different setting.
    """
    from fastapi.testclient import TestClient

    from permitflow.api.main import app
    from permitflow.config import get_settings

    monkeypatch.setenv("PROPERTY_RECORDS_WSDL", soap_service)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture
def committed_parties() -> dict[str, UUID]:
    """Parties visible to the API's own connections.

    The `parties` fixture holds its rows in an open transaction, which the API cannot see
    because it opens its own. API tests need the rows committed first.
    """
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applicant (full_name, email) VALUES ('API Applicant','api@example.com') RETURNING id"
        )
        applicant_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO contractor (license_number, business_name)
               VALUES ('VA-CL-004182','Harlowe Building Group LLC') RETURNING id"""
        )
        contractor_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code)
               VALUES ('14-220-118','1408 Aldergate Ln','R-90') RETURNING id"""
        )
        parcel_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO parcel (apn, situs_address, zoning_code, stop_work_order)
               VALUES ('05-201-330','9 Quarry Lane','R-60', true) RETURNING id"""
        )
        blocked_parcel_id = cur.fetchone()["id"]

    return {
        "applicant_id": applicant_id,
        "contractor_id": contractor_id,
        "parcel_id": parcel_id,
        "blocked_parcel_id": blocked_parcel_id,
    }


@pytest.fixture
def soap_failures(soap_service: str):
    """Reset the mock's injected failures around each test that touches it."""
    from permitflow.integrations.soap_mock.server import failures

    failures.outage = False
    failures.fail_next = 0
    failures.delay_seconds = 0.0
    failures.request_log.clear()
    yield failures
    failures.outage = False
    failures.fail_next = 0
    failures.delay_seconds = 0.0
