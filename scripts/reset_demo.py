"""Clear every case row, leaving reference data in place.

    python scripts/reset_demo.py

Applications, reviews, documents, escalations, clock pauses, integration logs, and the
audit trail all go. Permit types, disciplines, staff, holidays, SLA policies, and document
types stay, because those come from `sql/003_seed.sql` at container init and are not case
data.

Clearing the audit trail needs the table owner. The append-only triggers reject TRUNCATE
for everybody else, including the role the application connects as.
"""

from __future__ import annotations

import sys

from permitflow.db import get_pool
from permitflow.demo import CASE_TABLES, case_row_count, reset_case_data


def main() -> None:
    try:
        with get_pool().connection() as conn:
            conn.autocommit = True
            before = case_row_count(conn)
            reset_case_data(conn)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"reset failed ({exc}); is the database up? run: docker compose up -d")

    print(f"Cleared {before} applications across {len(CASE_TABLES)} tables.")
    print("Reference data left in place.")


if __name__ == "__main__":
    main()
