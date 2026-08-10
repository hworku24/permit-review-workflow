"""Database access.

Thin on purpose. There is no ORM here because the interesting SQL in this project is the
reporting views and the business day functions, and an ORM would put a layer between the
code and the thing worth showing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from functools import lru_cache

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings
from .process.sla import HolidayCalendar


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    settings = get_settings()
    return ConnectionPool(
        settings.permitflow_db_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """A connection inside a transaction, committed on clean exit.

    Every state change in the engine runs inside one of these. The denormalized
    `application.status` and the `status_history` row it corresponds to are written in the
    same transaction, which is what makes the guarantee in US-03 hold.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


def load_holiday_calendar(conn: psycopg.Connection) -> HolidayCalendar:
    """Read the published municipal holiday calendar.

    Loaded per operation rather than cached at import, because the calendar is data the
    department maintains and a long-running process should pick up an added holiday
    without a restart.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT holiday_date FROM holiday")
        rows = cur.fetchall()
    return HolidayCalendar.from_iterable(_as_date(r["holiday_date"]) for r in rows)


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def next_application_number(conn: psycopg.Connection, year: int) -> str:
    """Human-facing identifier, BLD-YYYY-NNNNN.

    Drawn from a sequence rather than from count(*) so two concurrent submissions cannot
    be handed the same number.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('application_number_seq') AS n")
        n = cur.fetchone()["n"]
    return f"BLD-{year}-{n:05d}"
