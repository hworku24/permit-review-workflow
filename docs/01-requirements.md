# Requirements: Building Permit Review Modernization

**Client:** City of Rivermont, Department of Permitting Services (DPS)
**Engagement:** Post-sales implementation, Phase 1
**Document owner:** Hewan Worku, Technical Consultant
**Status:** Baselined
**Last revised:** August 2026

> Rivermont is a fictional jurisdiction. The zoning provisions, fee schedules, and review
> timelines in this project are modeled on patterns common to mid-size US municipalities
> but do not represent any real city's code.

## 1. Business context

DPS reviews roughly 4,200 building permit applications a year across residential and
commercial work. An application arrives, gets screened for completeness, then goes to
between one and four specialist reviewers depending on the scope of work. Each reviewer
either approves, or writes up deficiencies the applicant has to correct and resubmit.
When every discipline has signed off, a supervisor issues the permit.

The department is measured publicly on cycle time. The city council adopted a service
standard in 2024 committing to a first review decision within 20 business days for
residential work and 30 for commercial. DPS has met that standard in about 61% of cases.

## 2. Current state

The process runs on a shared network drive and a departmental email inbox.

- Applications arrive as PDF packets attached to email. An intake clerk saves them to a
  folder named after the address.
- Assignment to reviewers happens in a weekly meeting and is recorded in a spreadsheet.
- Reviewers write deficiency letters in Word and email them to the applicant directly.
- Status is whatever the clerk last typed in the spreadsheet.
- Parcel and zoning lookups are done by hand against the county assessor's web portal.
- Contractor license checks are done by hand against the state licensing database.

### Pain points confirmed in discovery

| # | Pain point | Evidence from discovery |
|---|---|---|
| P1 | No reliable status. Applicants call to ask where their permit is and staff cannot answer without opening three files. | Front desk logs 60 to 80 status calls a week |
| P2 | The SLA clock is not tracked, so breaches are found after the fact | The 61% compliance figure is reconstructed quarterly by hand |
| P3 | Work sits in queues nobody is watching. Median 6 days between intake completion and first reviewer touch | Sampled 40 cases from Q1 |
| P4 | Assignment is uneven. Two of the seven reviewers carried 44% of Q1 volume | Assignment spreadsheet |
| P5 | Incomplete submissions consume review capacity before anyone notices they are incomplete | 31% of applications are returned at least once for missing documents |
| P6 | No audit trail. When a decision is appealed, the department cannot reconstruct who changed what and when | Legal flagged this after the 2025 Aldergate appeal |
| P7 | Reviewers answer the same zoning questions repeatedly and sometimes inconsistently | Raised unprompted by four of six reviewers interviewed |

## 3. Scope

### In scope for Phase 1

Intake through permit issuance for building permits. The case lifecycle, reviewer
assignment, SLA tracking, deficiency handling, the two system integrations required at
intake, and the AI assistance described in section 5.

### Out of scope for Phase 1

Inspections and certificate of occupancy, fee calculation and payment processing, the
public applicant-facing portal, trade permits (electrical, plumbing, mechanical) filed
separately from a building permit, and the appeals board workflow. Phase 1 records that an
appeal was filed and freezes the case. It does not manage the hearing.

## 4. Functional requirements

### Intake

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | The system shall accept a permit application capturing applicant, contractor, parcel, permit type, scope of work narrative, declared valuation, and affected square footage. | Must |
| FR-02 | On submission the system shall retrieve parcel record, zoning district, and ownership from the county Property Records system and attach them to the application. | Must |
| FR-03 | On submission the system shall verify the contractor's license number, status, and expiration against the state Business Licensing database. | Must |
| FR-04 | The system shall derive the required document checklist from the permit type and the scope of work, and shall block advancement out of intake while any required document is missing. | Must |
| FR-05 | The system shall allow an intake clerk to return an application to the applicant as incomplete, with itemized reasons. | Must |
| FR-06 | The system shall reject submission for a parcel with an open stop-work order. | Should |

### Review

| ID | Requirement | Priority |
|---|---|---|
| FR-07 | The system shall route an application to one or more discipline review queues (zoning, structural, fire, environmental) based on permit type and scope. | Must |
| FR-08 | Discipline reviews shall run in parallel. The application shall not advance until every assigned discipline reaches a terminal outcome. | Must |
| FR-09 | The system shall assign each review task to the least loaded active reviewer holding the required discipline certification. | Must |
| FR-10 | A reviewer shall be able to approve, approve with conditions, or record deficiencies against their assigned task. | Must |
| FR-11 | Recording one or more deficiencies shall move the application to Revisions Requested and pause the SLA clock until the applicant resubmits. | Must |
| FR-12 | On resubmission the system shall reopen only the discipline tasks that recorded deficiencies. Disciplines that already approved shall not be re-reviewed. | Must |
| FR-13 | A supervisor shall be able to reassign a review task, and the reassignment shall be recorded with actor and reason. | Must |

### SLA and escalation

| ID | Requirement | Priority |
|---|---|---|
| FR-14 | The system shall track an SLA clock per application and per review task, measured in business days against the published municipal holiday calendar. | Must |
| FR-15 | The clock shall pause while an application waits on the applicant and resume on resubmission. | Must |
| FR-16 | The system shall raise an escalation when a task passes 80% of its allowance and a second when it breaches. | Must |
| FR-17 | Escalations shall be visible on a supervisor queue ordered by time past due. | Must |

### Decision and audit

| ID | Requirement | Priority |
|---|---|---|
| FR-18 | A supervisor shall be able to issue or deny a permit only when every assigned discipline has reached a terminal outcome. | Must |
| FR-19 | A denial shall require a recorded reason referencing at least one deficiency or code provision. | Must |
| FR-20 | The system shall write an immutable audit record for every state transition, assignment, document action, integration call, and AI recommendation, capturing actor, timestamp, before value, and after value. | Must |
| FR-21 | Audit records shall not be editable or deletable through any application path. | Must |

### Reporting

| ID | Requirement | Priority |
|---|---|---|
| FR-22 | The system shall report median and 90th percentile cycle time by permit type and by month. | Must |
| FR-23 | The system shall report SLA compliance rate against the council standard. | Must |
| FR-24 | The system shall report open workload per reviewer and per discipline. | Should |

## 5. AI requirements

The client asked for AI assistance in two places. Both were scoped narrowly on purpose,
because the department's tolerance for an incorrect automated decision is effectively zero.

| ID | Requirement | Priority |
|---|---|---|
| AI-01 | The system shall extract structured fields (work type, valuation, square footage, occupancy class, disciplines implicated) from the free-text scope of work narrative and present them to the intake clerk as a pre-filled draft. | Must |
| AI-02 | Extracted fields shall carry a per-field confidence score. Fields below the configured threshold shall be presented as unfilled rather than as a low-confidence guess. | Must |
| AI-03 | The system shall recommend the discipline routing for an application and shall show the reasoning behind the recommendation. | Must |
| AI-04 | No AI output shall directly cause a state transition, a denial, or a return for incompleteness. Every AI output is a recommendation a named human accepts or overrides. | Must |
| AI-05 | The system shall answer reviewer questions about the zoning ordinance with answers grounded in retrieved ordinance text, quoting the provision verbatim with its section citation. | Must |
| AI-06 | An answer that cannot be grounded in retrieved text shall be withheld rather than generated. | Must |
| AI-07 | Every AI recommendation and every human override shall be written to the audit log with the model, prompt version, and retrieved sources. | Must |

### Rationale for AI-04 and AI-06

P7 is a knowledge distribution problem, so retrieval with citation solves it. P5 is a data
entry problem, so extraction solves it. Neither is a judgment problem, and the failure
cost of an ungrounded answer to a code question is a wrongly issued permit. The design
therefore puts the model in the drafting and retrieval path and keeps it out of the
decision path entirely.

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-01 | Role based access with four roles: applicant, intake clerk, discipline reviewer, supervisor. A reviewer shall only act on tasks assigned to their discipline. |
| NFR-02 | An integration failure at intake shall degrade rather than block. The application is accepted and the lookup is queued for retry, with the gap visible to the clerk. |
| NFR-03 | External calls shall apply a timeout, bounded retry with backoff, and a circuit breaker that stops calling a failing dependency. |
| NFR-04 | The audit log shall be append only, enforced at the database level rather than in application code. |
| NFR-05 | Business day math shall be correct across weekends, the published holiday calendar, and clock pauses. This is the single most tested piece of logic in the system. |
| NFR-06 | The system shall run locally with no external API keys, using a deterministic offline path for AI features so the build is reproducible and testable. |

## 7. Assumptions

1. The county Property Records SOAP service is available and its WSDL is stable. Rivermont
   does not control it and cannot request changes to it.
2. The state Business Licensing database is read only to DPS and is reachable over a
   direct database connection. There is no API in front of it.
3. The zoning ordinance is published as text and is versioned by adoption date.
4. Reviewer discipline certifications are maintained in the HR system and synced nightly.
   Phase 1 treats them as reference data.
5. Volume stays under 10,000 applications a year, so the design does not need horizontal
   sharding.

## 8. Open items

| # | Item | Owner | Needed by |
|---|---|---|---|
| O1 | Confirm whether the 20 business day standard starts at submission or at intake completion. Materially changes reported compliance. | DPS Director | Before UAT |
| O2 | Confirm retention period for audit records. Legal suggested 7 years, unconfirmed. | City Legal | Before go-live |
| O3 | Decide whether approve-with-conditions requires supervisor countersignature. | DPS Director | Sprint 3 |
