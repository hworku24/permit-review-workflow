# PermitFlow

A building permit review system for a municipal permitting department. It handles the part
of the job that spreadsheets are bad at: a case that moves through eight states, gets
reviewed by four specialists working in parallel, stops and starts a clock that only counts
business days, pulls data from two systems nobody in the department controls, and has to be
reconstructable line by line when a decision gets appealed two years later.

The client is the City of Rivermont, which does not exist. The zoning provisions, fee
thresholds, and review timelines are modeled on patterns common to mid-size US
municipalities. The engineering problems are real.

Two things worth stating plainly. The repository was initialized after the code was
written, so the commits are sequenced to follow the build order in
[docs/05-user-stories.md](docs/05-user-stories.md) rather than a real calendar. And no
part of this has ever run in a municipal department. Every number in this README comes
from the seeded case generator described below.

## Why this exists

Most of the code I write moves data from one shape to another. This is a different problem.
The hard part is not any single operation, it is that the state of a permit application is
the product of a dozen rules that interact, and every one of them has a way of being quietly
wrong.

A few examples of what that looks like in practice:

- The department reports its cycle time to the city council. If the clock counts the three
  weeks an applicant spent redrawing a site plan, the number the council sees is wrong and
  nobody notices, because it looks reasonable.
- If a structural reviewer finds a problem, the applicant fixes it and resubmits. The zoning
  reviewer who already approved should not review it again. Getting that wrong wastes review
  capacity the department does not have.
- The county assessor's system goes down. If that stops Rivermont accepting permit
  applications, the department has converted somebody else's outage into its own service
  failure, and the front desk takes the calls.

## Running it

Postgres and Docker are the only prerequisites. Nothing here needs an API key or an account.

```bash
docker compose up -d
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

That starts two databases: PermitFlow's own, and a separate one standing in for the state
licensing replica. The schema, views, and reference data load automatically.

Start the mock county SOAP service and the API:

```bash
.venv/bin/uvicorn permitflow.integrations.soap_mock.server:app --port 8081
```

```bash
PYTHONPATH=. .venv/bin/uvicorn permitflow.api.main:app --reload --port 8000
```

Then generate a year of history and read the reports:

```bash
PYTHONPATH=. .venv/bin/python scripts/seed_cases.py --cases 120
```

The seeder drives the real engine with an injected clock rather than writing rows directly,
so the history it produces obeys the same guards and writes the same audit trail as
production traffic. A recent run of 60 cases:

```
decided  compliance  mean_net  median_net  p90_net  mean_gross  mean_wait
     60       71.7%      18.3          18     28.1        28.2        9.8
```

Mean gross minus mean applicant wait equals mean net, which is the arithmetic the whole SLA
design exists to get right. The 71.7% compliance figure is deliberately in the same range as
the 61% the fictional department was hitting before, because a demo where everything passes
does not demonstrate anything.

Run the tests:

```bash
PYTHONPATH=. .venv/bin/python -m pytest
```

233 tests, against a real Postgres. The database behaviour matters too much to fake: the
business day functions, the append-only audit triggers, the partial unique indexes, and the
reporting views are all things a substitute would let me get wrong.

## How it fits together

```
                    FastAPI
                       |
              +--------+--------+
              |                 |
        process engine       AI layer
     (states, SLA clock,   (retrieval,
      assignment, audit)    extraction)
              |                 |
        +-----+-----+           |
        |           |           |
   PostgreSQL   integrations    corpus
   (schema +    (SOAP, direct   (zoning
    views)       DB connection)  ordinance)
```

`permitflow/process/` is where the interesting logic is. `states.py` holds the transition
table with no database access in it at all, so it can be read and argued about in a client
workshop. `engine.py` is the only thing that moves a case, and it owns three guarantees:
nothing transitions that the state machine and the actor's role do not both permit, the
denormalized status and the history row are written in one transaction, and the SLA clock
pauses as a consequence of the target state rather than as something each action remembers
to do.

That last one is worth dwelling on. An action that forgets to pause the clock produces a
wrong compliance number, so no action is trusted to remember.

## Decisions I would defend in a review

**Review tasks are rows, not columns.** The shortcut is four booleans on the application:
`zoning_approved`, `structural_approved`, and so on. It does not survive the requirements.
An assignee per discipline, an SLA clock per discipline, a round counter per discipline, and
reassignment history per discipline are all attributes of the review, so the review is an
entity. Adding a fifth discipline is then two rows of configuration and no schema change.

**The audit log is append-only in the database, not in the application.** A convention the
application follows is a convention the next developer breaks. Triggers that raise
`insufficient_privilege` on UPDATE, DELETE, and TRUNCATE cannot be broken by application
code at all. The test suite has to disable them as the table owner just to reset between
tests, which is the clearest demonstration I could give that the application role never can.

**The integration log has no foreign key, on purpose.** It is written on a separate
autocommit connection so an attempt log survives the rollback of the transaction that made
the call. A foreign key would make that write take a lock on the application row that
conflicts with the `FOR UPDATE` the calling transaction already holds. One side waits in
Postgres, the other in application code, and Postgres never sees a cycle to break. I found
this by hanging the API for two minutes and reading a thread dump.

**Business day math exists twice and a test forces it to agree.** The engine needs it in
Python, the reporting views need it in SQL. Two copies of one rule is a real risk, and the
failure is quiet: the engine would stamp a due date the compliance report disagrees with.
`tests/test_sla_parity.py` runs both implementations over generated date pairs spanning
weekends, holidays, and the turn of the year, and fails if they ever differ.

**"Could not check" is a different answer from "checked and it passed."** When the licensing
replica is unreachable, the contractor's licence is recorded as UNVERIFIED, never ACTIVE.
Collapsing the two is how a database outage quietly waves a revoked licence through intake,
and it is the kind of defect that only surfaces during an appeal.

## Integrations

Neither external system belongs to Rivermont, and neither has a modern interface. That is
the ordinary condition of public sector integration work, so the design accommodates it
rather than fighting it.

| System | Protocol | Notes |
|---|---|---|
| County property records | SOAP 1.1 over HTTP | A real zeep client against a published WSDL. The mock service speaks the same contract, so pointing at the county is one environment variable. |
| State contractor licensing | Direct database connection | No API exists. Jurisdictions get a read only account against a replica. Hand-written SQL against a schema Rivermont does not own, with `char(12)` padding and a status column that lags the expiry date. |

Both go through one resilience policy: bounded retry with exponential backoff, a circuit
breaker so a sustained outage stops costing every applicant three timeouts, and an
attempt-level log so an integration dispute is settled with records rather than
recollection. A definitive answer, such as a parcel that genuinely does not exist, is not
retried, because retrying a definitive answer is three times the latency for the same
result.

Every failure path has a test. A resilience policy you cannot demonstrate failing is a
resilience policy nobody should believe.

## What the AI does, and what it is not allowed to do

Two features, both narrow on purpose, because the department's tolerance for an incorrect
automated decision is effectively zero.

**Intake triage** reads the applicant's scope of work narrative and drafts the structured
fields a clerk would otherwise retype. Every field carries a confidence and the verbatim
span of the narrative it came from. A field below the threshold is presented blank rather
than as a guess, because a blank costs the clerk twenty seconds and a confidently wrong
value gets accepted at a glance and becomes the record.

**Ordinance questions** get answered with the ordinance's own words. The retrieval is
lexical BM25 over the zoning code, which is the right default for a legal corpus: a reviewer
asking about "rear setback in R-90" wants the section that literally says those words.

The design decision that matters is in how a citation is produced. The model never types a
quote. Retrieval returns a section number, code reads the text back out of the corpus by
that number, and a verification pass asserts the quote appears byte for byte in the source
before the answer is returned. A model that searches, quotes, and then grades its own quote
agrees with itself. Retrieval and provenance are things a database does correctly, so they
stay in code. Judgement is the only part worth a model.

Grounding is checked by how much of the question's information content a section actually
contains, weighted by inverse document frequency. Plain term overlap is not enough, and the
failure is easy to reproduce: "how many parking spaces does a helipad require" overlaps the
residential parking section on "parking" and "spaces" and looks well supported, even though
the ordinance says nothing about helipads. Weighting by rarity fixes it, and the question
comes back unanswerable, which is the correct outcome.

Everything the AI produces is a recommendation that a named human accepts or overrides, and
both the recommendation and the decision are written to the audit log with the model and
prompt version. There is no endpoint in the AI layer that changes an application's status.

It runs with no credentials by default. The offline provider is rule-based, deterministic,
and genuinely useful rather than a stub, which is what lets CI test the AI behaviour at all.
A hosted Claude path is available when a key is configured.

## Documentation

The `docs/` directory holds the consulting side of the work, written the way it would be for
an actual engagement:

| Document | What it covers |
|---|---|
| [01-requirements.md](docs/01-requirements.md) | Business context, current state, discovery pain points, numbered functional and non-functional requirements |
| [02-process-map.md](docs/02-process-map.md) | Lifecycle and task state machines, swimlane view, where the AI sits, escalation timing |
| [03-data-model.md](docs/03-data-model.md) | ERD, the design decisions worth defending, key tables, reporting views |
| [04-integration-spec.md](docs/04-integration-spec.md) | Both external contracts, failure handling, intake sequence |
| [05-user-stories.md](docs/05-user-stories.md) | Stories by sprint, with acceptance criteria written so they read as test cases |
| [06-traceability.md](docs/06-traceability.md) | Every requirement traced to a design artifact, a module, and a test |

The traceability matrix is checked rather than asserted. Every module path and test name in
it was verified to exist.

## Layout

```
permitflow/
  process/          states, engine, SLA clock, assignment, checklist, routing, escalation
  integrations/     SOAP client, licensing client, shared resilience policy, mock service
  ai/               retrieval, grounded answers, triage, provider abstraction, recording
  api/              FastAPI routes, schemas, actor resolution, error mapping
  db.py audit.py config.py errors.py
sql/                schema, reporting views, reference data, legacy licensing schema
corpus/             the zoning ordinance the retrieval layer reads
docs/               requirements through traceability
scripts/            case history generator
tests/              233 tests against a real database
```

## Scope

Phase 1 covers intake through issuance. Inspections, certificate of occupancy, fee
calculation and payment, the public applicant portal, and appeals hearing management are all
out of scope and are listed as such in the requirements. The API supports a portal, the UI
would be Phase 2.

The `X-Actor` header is not authentication. A real deployment puts the city's SSO in front
of this. It is called out in the code rather than hidden, because a header-based identity
that looks like auth is worse than one that obviously is not.
