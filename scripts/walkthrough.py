"""Drive a permit from screening to issuance over HTTP only.

    python scripts/walkthrough.py [base-url]

No SQL anywhere in this file. Every step is an endpoint a person or another system could
call, which is the test: if the workflow needs a psql session to finish, it is not finished.
Run it after seeding, against a local instance or the deployed one.

The applicant, parcel, and contractor rows are prerequisites the seeded data supplies. In a
real deployment they arrive from the applicant portal, which is Phase 2 and documented as out
of scope, and from the county during enrichment.

It consumes the case it works on, so reseed before running it again.
"""

import os
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PERMITFLOW_URL", "http://localhost:8000")
client = httpx.Client(base_url=BASE, timeout=30)
failures: list[str] = []


#: The deployed copy sits behind the shared passphrase, so a gate cookie can be passed in.
GATE = os.environ.get("PERMITFLOW_GATE", "")


def call(method: str, path: str, actor: str, expect: int = 200, **kw):
    headers = {"X-Actor": actor}
    if GATE:
        headers["Cookie"] = f"permitflow_gate={GATE}"
    r = client.request(method, path, headers=headers, **kw)
    if r.status_code != expect:
        failures.append(f"{method} {path} -> {r.status_code} (wanted {expect}) {r.text[:200]}")
        return None
    return r.json() if r.content else {}


def step(n: str) -> None:
    print(f"\n{n}")


def ok(msg: str) -> None:
    print(f"   {msg}")


# ---------------------------------------------------------------------------
step("1. find the case sitting at intake screening")
screening = call("GET", "/queues/intake", "mcarrero")
if not screening:
    sys.exit("no intake queue")
case = screening[0]
app_id = case["application_id"]
ok(f"{case['application_number']}, {case['permit_type_name']}")
ok(f"missing documents: {case['missing_document_count']}, licence verified: {case['license_verified']}")

# ---------------------------------------------------------------------------
step("2. run AI triage and decide it as the clerk")
triage = call("POST", f"/ai/applications/{app_id}/triage", "mcarrero")
if triage:
    drafted = triage["field_extraction"]["accepted"]
    withheld = triage["field_extraction"]["withheld_below_threshold"]
    ok(f"drafted {len(drafted)} field(s): {', '.join(drafted) or 'none'}")
    ok(f"held back {len(withheld)}: {', '.join(w['name'] for w in withheld) or 'none'}")
    for w in withheld:
        if "value" in w:
            failures.append(f"a withheld field leaked its value: {w}")
    rec = triage["field_extraction"]["recommendation_id"]
    call("POST", f"/ai/recommendations/{rec}/decide", "mcarrero",
         json={"accepted": True})
    ok("clerk accepted the draft")

# ---------------------------------------------------------------------------
step("3. supply the outstanding documents")
documents = call("GET", f"/applications/{app_id}/documents", "mcarrero")
missing = [d["document_type_code"] for d in documents if d["status"] == "MISSING"]
ok(f"outstanding: {', '.join(missing)}")
for code in missing:
    call("PUT", f"/applications/{app_id}/documents/{code}", "applicant-walkthrough",
         json={"filename": f"{code.lower()}.pdf"})
ok(f"uploaded {len(missing)}")

# ---------------------------------------------------------------------------
step("4. accept intake, which opens the discipline reviews")
accepted = call("POST", f"/applications/{app_id}/intake/accept", "mcarrero", json={})
if accepted:
    ok(f"status is now {accepted['status']}")

tasks = call("GET", f"/applications/{app_id}/tasks", "mcarrero")
ok(f"{len(tasks)} review task(s): {', '.join(t['discipline_code'] for t in tasks)}")

# ---------------------------------------------------------------------------
step("5. one reviewer finds a problem, the rest approve")
first, rest = tasks[0], tasks[1:]
call("POST", f"/tasks/{first['review_task_id']}/start", first["reviewer_username"], json={})
call("POST", f"/tasks/{first['review_task_id']}/deficiencies", first["reviewer_username"],
     json={"deficiencies": [{"code_reference": "IRC R502.3.1",
                             "description": "Floor joist span exceeds the allowable table value",
                             "severity": "MAJOR"}]})
ok(f"{first['discipline_code']} raised a deficiency")
for t in rest:
    call("POST", f"/tasks/{t['review_task_id']}/start", t["reviewer_username"], json={})
    call("POST", f"/tasks/{t['review_task_id']}/approve", t["reviewer_username"], json={})
    ok(f"{t['discipline_code']} approved")

detail = call("GET", f"/applications/{app_id}", "mcarrero")
ok(f"status is now {detail['status']}, clock paused: {detail['clock_paused']}")
if not detail["clock_paused"]:
    failures.append("the clock should be paused while the case sits with the applicant")

# ---------------------------------------------------------------------------
step("6. the applicant resubmits, and only the deficient discipline reopens")
call("POST", f"/applications/{app_id}/resubmit", "applicant-walkthrough", json={})
all_tasks = call("GET", f"/applications/{app_id}/tasks", "mcarrero")
open_now = [t for t in all_tasks if t["status"] in ("PENDING", "ASSIGNED", "IN_PROGRESS")]
ok(f"round 2 opened for: {', '.join(t['discipline_code'] for t in open_now)}")
if len(open_now) != 1 or open_now[0]["discipline_code"] != first["discipline_code"]:
    failures.append(
        f"round 2 should reopen only {first['discipline_code']}, got "
        f"{[t['discipline_code'] for t in open_now]}"
    )
else:
    ok("the disciplines that already approved were not asked again")

for t in open_now:
    call("POST", f"/tasks/{t['review_task_id']}/start", t["reviewer_username"], json={})
    call("POST", f"/tasks/{t['review_task_id']}/approve", t["reviewer_username"], json={})
ok("round 2 approved")

# ---------------------------------------------------------------------------
step("7. the supervisor issues it")
issued = call("POST", f"/applications/{app_id}/issue", "dhollis", json={})
if issued:
    ok(f"status is now {issued['status']}")
    if issued["status"] != "ISSUED":
        failures.append(f"expected ISSUED, got {issued['status']}")

# ---------------------------------------------------------------------------
step("8. the audit trail reconstructs the whole thing")
history = call("GET", f"/applications/{app_id}/history", "dhollis")
transitions, audit = history["transitions"], history["audit"]
ok(f"{len(transitions)} status transitions and {len(audit)} audit rows")
for h in transitions:
    ok(f"  {h['occurred_at'][:16]}  {h['from_status'] or 'created'} -> {h['to_status']}  by {h['actor']}")

# ---------------------------------------------------------------------------
step("9. the clock arithmetic")
cycle = call("GET", "/reports/cycle-time", "dhollis", params={"limit": 200})
row = next((r for r in cycle if r["application_number"] == case["application_number"]), None)
if row:
    ok(f"gross {row['gross_business_days']}, wait {row['applicant_wait_business_days']}, "
       f"net {row['net_business_days']}, rounds {row['review_rounds']}")
    if row["gross_business_days"] - row["applicant_wait_business_days"] != row["net_business_days"]:
        failures.append("gross minus applicant wait did not equal net on this case")
    else:
        ok("gross minus applicant wait equals net")
    # The wait is business days, and this walkthrough resubmits within seconds, so zero is
    # the right answer here. What is worth checking is that the pause was recorded at all:
    # a clock that never stopped and a clock that stopped for no measurable time produce the
    # same number, and only one of them is correct.
    if row["review_rounds"] < 2:
        failures.append("this case went back to the applicant, so it should show two rounds")
    else:
        ok(f"{row['review_rounds']} rounds, and the clock stopped between them")

# ---------------------------------------------------------------------------
step("10. the ordinance still declines what it cannot answer")
answer = call("GET", "/ai/ordinance", "pvasquez",
              params={"q": "How many parking spaces does a helipad require?"})
if answer:
    ok(f"answered: {answer['answered']}")
    if answer["answered"]:
        failures.append("the helipad question should come back unanswerable")

good = call("GET", "/ai/ordinance", "pvasquez", params={"q": "What is the rear setback in R-90?"})
if good and good["citations"]:
    ok(f"{good['citations'][0]['section']}: \"{good['citations'][0]['quote'][:60]}\"")

# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
if failures:
    print(f"{len(failures)} problem(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("the full workflow completed over HTTP with no database access")
