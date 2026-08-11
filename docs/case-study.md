# PermitFlow

Building permit case management for a municipal permitting department.

**Live:** https://d2z5bkq0t31r0m.cloudfront.net
**Code:** https://github.com/hworku24/permit-review-workflow

**Everything below is simulated.** The City of Rivermont does not exist. There was no
client, no engagement, and no department. The zoning provisions, fee thresholds, and review
timelines are modeled on patterns common to mid-size US municipalities, and every figure
comes from a case generator that drives the real system with a controlled clock. What is
real is the engineering: the rules, the integrations, the data model, and the tests.

---

## The client and the problem

Rivermont's permitting department reviews about 500 building permits a year with four
specialist reviewers, a supervisor, and two intake clerks. Applications arrive by email and
counter. The department tracks them in a shared spreadsheet.

The council asks for one number each quarter: what share of permits were decided inside the
adopted standard, which is 20 business days for residential and 30 for commercial.

Three things were wrong with the way that number was produced, and all three are the same
kind of wrong. They are quiet.

**The clock counted time the department did not control.** When a reviewer sent an
application back for a redrawn site plan, the spreadsheet kept counting. The applicant's
three weeks landed in the department's cycle time. The reported figure was worse than the
department's actual performance, by an amount nobody could quantify, and it looked plausible
enough that nobody questioned it.

**A resubmission restarted everybody.** One discipline finding a problem sent the whole
application back to all four, including the two who had already approved. In a department
with no spare review capacity, that was capacity spent re-reading work already done.

**Nobody could reconstruct a decision.** When a 2025 denial on Aldergate Lane was appealed,
the department could not establish who had changed the zoning determination or when. The
spreadsheet had been edited in place for eighteen months.

---

## What was built

A case management system for intake through issuance. Six screens for department staff, a
JSON API behind them, and a process engine that is the only thing permitted to move a case.

**The clock stops when the department is waiting on the applicant.** It is not something
each action remembers to do; the clock pauses as a consequence of the state the case moves
into, so an action that forgets cannot exist. Reported cycle time is now three numbers:
gross elapsed, applicant wait, and net. Gross minus wait equals net, and that identity is
asserted on every decided case and not on the averages, because an average can hold while
individual rows are wrong.

**Reviews are entities, not columns.** Each discipline has its own assignee, its own clock,
its own round counter, and its own reassignment history. A resubmission opens round two only
for the disciplines that raised something. The reviewer who already approved does not review
it again.

**The audit trail cannot be edited by the application.** Append-only is enforced by database
triggers, not by a convention the next developer breaks. The test suite has to disable those
triggers as the table owner just to reset between tests, which is the clearest available
proof that the application role never can.

**Two integrations with systems the city does not own.** The county assessor's parcel
records over SOAP, and the state's contractor licensing over a direct read-only database
connection, because no API exists and jurisdictions are handed a replica account. Both go
through one resilience policy: bounded retry, a circuit breaker, and an attempt-level log
written on a separate connection so it survives the rollback of the transaction that made
the call.

**AI in two narrow places, neither of which can move a case.** Intake triage drafts the
structured fields a clerk would otherwise retype. Reviewers can ask the zoning ordinance a
question and get the ordinance's own words back.

---

## The decisions worth explaining

**A licence that could not be checked is not a licence that passed.** When the state replica
is unreachable, the contractor's licence is recorded UNVERIFIED, never ACTIVE. Collapsing
those two is how a database outage quietly waves a revoked licence through intake, and it is
the class of defect that only surfaces during an appeal. The same reasoning shows up in the
county integration: an outage at the county does not stop Rivermont accepting applications,
because converting somebody else's outage into your own service failure is a choice.

**The model never types a quote.** Retrieval returns a section number, code reads the text
back out of the corpus by that number, and a verification pass asserts the quote appears
byte for byte in the source before the answer is shown. A model that searches, quotes, and
then grades its own quote agrees with itself. Retrieval and provenance are things a database
does correctly, so they stay in code; judgement is the only part worth a model.

**A low-confidence field is shown blank, with the words the guess came from.** A blank costs
a clerk twenty seconds. A confidently wrong value gets accepted at a glance and becomes the
record, and nobody reads the narrative again.

**Configuration changes behave differently on purpose.** A supervisor can add a review
discipline, change an SLA allowance, or add a required document without a developer. A new
discipline reaches new applications only, because routing is decided at intake and reopening
it on a case whose reviewers have signed off would rewrite a decision made under the old
rules. An allowance reaches tasks opened afterwards, because it is written onto the task
when it opens and a supervisor should not be able to make a reviewer late by editing
configuration underneath them. A document requirement reaches open cases at their next
resubmission, because the checklist is rebuilt whenever a case arrives or comes back.

**The council standard is retroactive, and the screen refuses to let that happen quietly.**
The compliance view applies the current standard to every case ever decided, so moving it
rewrites what the department has already reported. Tightening residential from 20 days to 12
turns 21 of 30 decided cases into 9. That change is still allowed, because councils do change
the standard, but it takes a second click, the confirmation shows the size of the movement
first, and the audit row carries the compliance figure before and after so somebody who was
not in the room can explain it later.

---

## Results

From a generated year of history, 120 cases driven through the real engine:

| | |
|---|---|
| Decided cases | 115 |
| Met the council standard | 72.2% |
| Mean review time, net of applicant wait | 18.3 business days |
| Mean applicant wait | 8.7 business days |
| Mean gross elapsed | 27.0 business days |

Reproducible exactly with `scripts/seed_demo.py --cases 120 --today 2026-08-11`. The history
is generated backwards from the run date so the newest cases are always days old, which means
the figures move if you leave the date unpinned. Pinning it is how a number in a document
stays checkable.

The 8.7 days of applicant wait is the finding. Under the old spreadsheet it was inside the
reported figure, which means the department was reporting about 27 days against a 20 day
standard and could not explain the gap. Separating it does not make anyone faster. It makes
the number the council sees describe the thing the council is asking about.

The compliance figure is deliberately not near 100%. A demonstration where everything passes
shows nothing, and the department's own baseline was 61%.

---

## How it was built

**Requirements first.** Seven discovery pain points with evidence, 24 functional and 7
non-functional requirements, three open items with owners and deadlines. Every user story's
acceptance criteria is written so it reads as a test case, because those became the tests.
Every requirement traces to a design artifact, a module, and a named test.

**Tested against a real database.** 383 Python tests and 20 Java tests, run in CI on every
push against real Postgres instances. The database behaviour matters too much to fake: the
business day functions, the append-only triggers, the partial unique indexes, and the
reporting views are all things an in-memory substitute would let you get wrong.

**Deployed.** One container on ECS Fargate behind a load balancer, CloudFront in front for
HTTPS, and a managed Postgres that no address on the internet can open a connection to. The
security group on the database has no CIDR rule at all, only the application's group.

---

## What is not built

Inspections, certificates of occupancy, fee calculation and payment, the public applicant
portal, and appeals hearing management. All out of scope, listed as such in the requirements
from the start, and not started.

The Spring service that holds the same licensing rules over a JDBC datasource has tests and
runs in CI, but nothing calls it. The application uses a direct connection instead. It is
documented as not being in the request path, and left out of the architecture diagram,
because a diagram that shows it would describe a request flow that does not happen.

Identity is the honest gap. The API names an actor in a header and the screens name one in a
cookie, and neither proves anything. The deployed copy adds one shared passphrase in front,
which is a gate on a public address and not a directory. A real deployment puts the city's
single sign-on there. Authorization is the part that is real: every action is checked against
the actor's role in the engine before it happens, so a rule cannot be skipped by driving the
engine from a script.
