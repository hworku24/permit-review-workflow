"""Prepare a fresh deployment database: schema, views, reference data, demo cases.

    PYTHONPATH=. python scripts/deploy_init.py

Run as a one-off task against the deployed database. The application image already carries
`sql/` and the seeding scripts, so the thing that applies the schema is the same artifact
that will serve traffic, and there is no second image to keep in step.

The database is not reachable from a laptop on purpose. Loading it from inside the same
network is why this exists at all, in place of a psql command in a runbook.

Safe to run twice. The schema and the reference data are applied only on a fresh database,
because `sql/001_schema.sql` and `sql/003_seed.sql` are first-run files and not migrations:
the CREATE TABLE statements have no IF NOT EXISTS and the inserts have no ON CONFLICT, which
is right for files whose job is to build an empty database and wrong for files you rerun.
The views are CREATE OR REPLACE, so those reload every time and a redeploy picks up a
changed view without a migration step. The demo cases are reseeded every time, since the
seeder resets case data first.

Changing reference data on an existing deployment is therefore not this script's job. That
is what the configuration screen is for, and the two would fight over the same rows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg

from permitflow.config import get_settings

SQL_FILES = [
    Path("sql/001_schema.sql"),
    Path("sql/002_views.sql"),
    Path("sql/003_seed.sql"),
]


def schema_present(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        return bool(
            conn.execute("SELECT to_regclass('public.application')").fetchone()[0]
        )


def apply_sql(dsn: str) -> None:
    """Views every time, schema and reference data only on a fresh database."""
    already = schema_present(dsn)
    for path in SQL_FILES:
        if not path.exists():
            sys.exit(f"{path} is missing from the image")
        if already and path.name.startswith(("001_", "003_")):
            print(f"skipping {path}, this database is not fresh", flush=True)
            continue
        print(f"applying {path}", flush=True)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(path.read_text())


def set_timezone(dsn: str) -> None:
    """The business day functions cast timestamptz to date, so the server's zone has to be
    the department's calendar. RDS defaults to UTC, and a database an hour off produces
    cycle times that are wrong by a day at the edges."""
    # ALTER DATABASE ... SET takes no bind parameters, so the value is inlined. It is a
    # module constant and never touches user input, which is the only reason that is fine.
    with psycopg.connect(dsn, autocommit=True) as conn:
        database = conn.info.dbname
        conn.execute(f"ALTER DATABASE \"{database}\" SET timezone = 'America/New_York'")
        print(f"timezone on {database} set to America/New_York", flush=True)


def build_licensing_replica(dsn: str) -> None:
    """Create the state licensing replica and load its schema.

    In reality this is another government's server and Rivermont has a read only account on
    it. In this deployment it is a second database on the same instance, reached with its
    own connection string and its own read only credential, which is what the integration
    code cares about. The saving is a second RDS instance for a demonstration; the code path
    is unchanged, and `docs/04-integration-spec.md` describes the real arrangement.

    Without this the licensing lookup has nothing to reach and every licence comes back
    UNVERIFIED. That is the resilience path working, and it also means the integration is
    never seen doing its job.
    """
    admin = psycopg.conninfo.conninfo_to_dict(dsn)
    password = admin["password"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'licensing_replica'"
        ).fetchone()
        if not exists:
            conn.execute("CREATE DATABASE licensing_replica")
            print("created licensing_replica", flush=True)

        role = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'permitflow_ro'"
        ).fetchone()
        if not role:
            conn.execute(f"CREATE ROLE permitflow_ro LOGIN PASSWORD '{password}'")
            print("created the read only role", flush=True)

    replica = dict(admin, dbname="licensing_replica")
    replica_dsn = psycopg.conninfo.make_conninfo(**replica)
    with psycopg.connect(replica_dsn, autocommit=True) as conn:
        conn.execute(Path("sql/legacy/001_licensing.sql").read_text())
        # SELECT and nothing else. The grant is the same one the state would issue, and it
        # is why a stray write fails here and never travels to somebody else's data.
        conn.execute("GRANT USAGE ON SCHEMA licensing TO permitflow_ro")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA licensing TO permitflow_ro")
        print("licensing replica loaded and granted read only", flush=True)


def seed(cases: int) -> None:
    print(f"seeding {cases} cases", flush=True)
    env = dict(os.environ, PYTHONPATH=f"{os.getcwd()}:{os.getcwd()}/scripts")
    result = subprocess.run(
        [sys.executable, "scripts/seed_demo.py", "--cases", str(cases), "--quiet"],
        env=env,
        capture_output=True,
        text=True,
    )
    print(result.stdout[-4000:], flush=True)
    if result.returncode != 0:
        print(result.stderr[-4000:], file=sys.stderr, flush=True)
        sys.exit("seeding failed")


def main() -> None:
    dsn = get_settings().permitflow_db_url
    # Never print the password. The host is useful in a log, the credential is not.
    with psycopg.connect(dsn) as conn:
        print(f"connected to {conn.info.host}/{conn.info.dbname}", flush=True)

    set_timezone(dsn)
    apply_sql(dsn)
    build_licensing_replica(dsn)
    seed(int(os.environ.get("DEMO_CASES", "120")))
    print("deployment database ready", flush=True)


if __name__ == "__main__":
    main()
