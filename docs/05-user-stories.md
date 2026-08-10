# User Stories

Grouped by sprint as they were planned. Acceptance criteria are written so they can be
read directly as test cases, which is how they ended up in `tests/`.

## Roles

| Role | Description |
|---|---|
| Applicant | Homeowner, contractor, or architect filing on behalf of an owner |
| Intake Clerk | Screens submissions for completeness, confirms routing |
| Discipline Reviewer | Certified in one or more of zoning, structural, fire, environmental |
| Supervisor | Issues and denies permits, reassigns work, owns the escalation queue |

---

## Sprint 1: Case exists and moves

### US-01: Submit an application
**As an** applicant **I want to** submit a permit application **so that** review can begin.

- Given a complete submission, when I submit, then an application is created in SUBMITTED
  with a generated application number in `BLD-YYYY-NNNNN` format.
- Given submission succeeds, then the SLA clock start is recorded as the submission time.
- Given a required field is missing, when I submit, then submission is rejected and no
  application number is consumed.

*Covers FR-01.*

### US-02: See where an application stands
**As an** applicant **I want to** see the current status and what it is waiting on **so
that** I stop calling the front desk.

- Given an application in any state, when I retrieve it, then I get the status, the party
  it is waiting on, and days remaining in the allowance.
- Given the application is paused waiting on me, then days remaining does not decrease.

*Covers P1, FR-14, FR-15.*

### US-03: Enforce legal transitions only
**As the** department **I want** the system to reject invalid transitions **so that** the
case record stays trustworthy.

- Given an application in DRAFT, when issue() is attempted, then it is rejected and the
  status is unchanged.
- Given any rejected transition, then nothing is written to `status_history`.
- Given any accepted transition, then `application.status` and the newest `status_history`
  row agree.

*Covers FR-20, and the denormalization guarantee in the data model.*

---

## Sprint 2: Intake

### US-04: Enrich from county records
**As an** intake clerk **I want** parcel and zoning data pulled automatically **so that** I
stop retyping it from the assessor portal.

- Given a valid APN, when the application is submitted, then zoning district, owner, and
  address are attached.
- Given the county service is down, then the application is still accepted and the parcel
  shows as unverified.
- Given a parcel with an open stop-work order, then submission is refused with that reason.

*Covers FR-02, FR-06, NFR-02.*

### US-05: Verify contractor license
**As an** intake clerk **I want** license status checked automatically **so that** expired
licenses are caught before review capacity is spent.

- Given an active license, then the application proceeds with the license recorded.
- Given an expired or missing license, then a deficiency is raised at intake.
- Given a suspended or revoked license, then the supervisor is notified and the
  application is held, without an automatic rejection.
- Given the licensing database is unreachable, then the license is recorded as unverified
  and never as valid.

*Covers FR-03.*

### US-06: Derive the document checklist
**As an** intake clerk **I want** the required document list derived from permit type and
scope **so that** I am not working from memory.

- Given a permit type, then the checklist matches the configured rules for that type.
- Given any required document is missing, then accept_intake() is refused and names the
  missing documents.
- Given all required documents are present, then accept_intake() succeeds.

*Covers FR-04.*

### US-07: Return an incomplete application
**As an** intake clerk **I want to** return an application with itemized reasons **so
that** the applicant knows exactly what to fix.

- Given at least one reason, then the application moves to RETURNED_INCOMPLETE and a clock
  pause opens.
- Given no reasons, then the return is refused.
- Given the applicant resubmits, then the pause closes and the clock resumes.

*Covers FR-05, FR-15.*

---

## Sprint 3: Review

### US-08: Route to disciplines
**As the** department **I want** applications routed to the right disciplines **so that**
the right specialists see the work.

- Given a permit type, then the disciplines configured as always-required are routed.
- Given a scope of work implicating an additional discipline, then that discipline is
  recommended and shown to the clerk for confirmation.
- Given routing is confirmed, then one PENDING task exists per discipline at round 1.

*Covers FR-07, AI-03.*

### US-09: Assign to the least loaded reviewer
**As a** supervisor **I want** work distributed evenly **so that** two people stop
carrying half the department.

- Given several certified active reviewers, then the task goes to the one with the fewest
  open tasks.
- Given a tie, then the tie breaks deterministically so the same input assigns the same way.
- Given no certified active reviewer exists, then the task stays PENDING and is escalated
  rather than assigned to someone uncertified.

*Covers FR-09, P4.*

### US-10: Review in parallel
**As a** reviewer **I want** my discipline to be independent **so that** I am not blocked
by another discipline's queue.

- Given four routed disciplines, then all four tasks are actionable at once.
- Given three are terminal and one is not, then the application stays UNDER_REVIEW.
- Given all four are terminal with none deficient, then the application moves to
  PENDING_DECISION.

*Covers FR-08.*

### US-11: Record deficiencies
**As a** reviewer **I want to** record deficiencies against code provisions **so that**
the applicant gets a specific and defensible correction list.

- Given at least one deficiency with a code reference, then the task becomes DEFICIENT.
- Given zero deficiencies, then record_deficiencies() is refused.
- Given all tasks terminal and at least one DEFICIENT, then the application moves to
  REVISIONS_REQUESTED and the clock pauses.

*Covers FR-10, FR-11.*

### US-12: Reopen only what was deficient
**As a** reviewer who already approved **I want to** not re-review **so that** the
department stops spending capacity twice.

- Given zoning approved and fire deficient, when the applicant resubmits, then a round 2
  task exists for fire only.
- Given round 2 exists, then the round 1 records are retained and readable.
- Given the same reviewer is still active, then the reopened task returns to them.

*Covers FR-12.*

---

## Sprint 4: SLA, decision, audit

### US-13: Track the clock correctly
**As a** supervisor **I want** SLA measured in business days net of applicant wait **so
that** the compliance number reported to council is defensible.

- Given a submission on a Friday, then Saturday and Sunday do not count.
- Given a municipal holiday in the interval, then it does not count.
- Given an application paused 10 calendar days, then those business days are excluded.
- Given the SQL function and the Python implementation are both run over the same date
  pairs, then they agree on every pair.

*Covers FR-14, FR-15, NFR-05.*

### US-14: Escalate before the breach
**As a** supervisor **I want** a warning before a task breaches **so that** I can reassign
in time to matter.

- Given a task passing 80% of its allowance, then a WARNING escalation is raised once.
- Given a task passing 100%, then a BREACH escalation is raised once.
- Given a task already escalated at a level, then no duplicate is raised.
- Given open escalations, then the supervisor queue orders them by time past due.

*Covers FR-16, FR-17.*

### US-15: Issue or deny
**As a** supervisor **I want to** issue or deny only when review is complete **so that** no
permit is issued over an unreviewed discipline.

- Given any non-terminal task, then issue() is refused.
- Given a denial without a recorded reason, then deny() is refused.
- Given issuance, then `decided_at` is set and the clock stops.

*Covers FR-18, FR-19.*

### US-16: Reconstruct any decision
**As** city legal **I want** a complete audit trail **so that** an appeal can be answered
with facts.

- Given any state transition, then an audit row records actor, timestamp, before, and after.
- Given an attempt to UPDATE or DELETE an audit row, then the database rejects it.
- Given an application, then its full history can be retrieved in order.

*Covers FR-20, FR-21, P6.*

---

## Sprint 5: AI assistance

### US-17: Pre-fill from the narrative
**As an** intake clerk **I want** structured fields drafted from the scope narrative **so
that** I am correcting a draft instead of typing from scratch.

- Given a narrative, then work type, valuation, square footage, occupancy class, and
  implicated disciplines are extracted with per-field confidence.
- Given a field below the confidence threshold, then it is presented blank rather than
  filled with a guess.
- Given the clerk accepts or overrides a field, then the suggestion, the decision, and the
  clerk are all written to the audit log.
- Given no model provider is configured, then the deterministic offline path runs and the
  feature still works.

*Covers AI-01, AI-02, AI-07, NFR-06.*

### US-18: Answer zoning questions with citations
**As a** reviewer **I want** ordinance answers quoted with section citations **so that** I
can verify the answer instead of trusting it.

- Given a question answerable from the ordinance, then the answer quotes the provision
  verbatim with its section number.
- Given a quote is returned, then that text appears byte for byte in the corpus.
- Given a question the corpus does not answer, then no answer is generated and the
  question is reported as unanswerable.
- Given any answer, then the retrieved sources are recorded.

*Covers AI-05, AI-06, AI-07, P7.*

### US-19: Keep AI out of the decision path
**As the** department **I want** every AI output to be a recommendation **so that** no
permit outcome is ever attributable to a model.

- Given any AI recommendation, then no state transition occurs as a direct result.
- Given a recommendation, then it stays pending until a named human accepts or overrides it.
- Given the audit log, then every recommendation records model, prompt version, and sources.

*Covers AI-04, AI-07.*
