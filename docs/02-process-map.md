# Process Map

Two views of the same process. The lifecycle diagram is the contract the code implements
in `permitflow/process/states.py`. The swimlane diagram is the version that goes on the
wall in a client workshop.

## 1. Application lifecycle

```mermaid
stateDiagram-v2
    [*] --> DRAFT

    DRAFT --> SUBMITTED : submit()
    SUBMITTED --> INTAKE_SCREENING : auto, on integration enrichment

    INTAKE_SCREENING --> RETURNED_INCOMPLETE : return_incomplete()<br/>guard: >=1 itemized reason
    RETURNED_INCOMPLETE --> INTAKE_SCREENING : resubmit()<br/>clock resumes

    INTAKE_SCREENING --> UNDER_REVIEW : accept_intake()<br/>guard: no missing required docs<br/>guard: >=1 discipline routed

    UNDER_REVIEW --> REVISIONS_REQUESTED : all tasks terminal<br/>and >=1 DEFICIENT
    REVISIONS_REQUESTED --> UNDER_REVIEW : resubmit()<br/>reopens only DEFICIENT tasks

    UNDER_REVIEW --> PENDING_DECISION : all tasks terminal<br/>and none DEFICIENT

    PENDING_DECISION --> ISSUED : issue()<br/>guard: supervisor role
    PENDING_DECISION --> DENIED : deny()<br/>guard: recorded reason

    DRAFT --> WITHDRAWN : withdraw()
    SUBMITTED --> WITHDRAWN : withdraw()
    INTAKE_SCREENING --> WITHDRAWN : withdraw()
    UNDER_REVIEW --> WITHDRAWN : withdraw()
    REVISIONS_REQUESTED --> WITHDRAWN : withdraw()
    RETURNED_INCOMPLETE --> EXPIRED : 180 days idle
    REVISIONS_REQUESTED --> EXPIRED : 180 days idle

    DENIED --> APPEAL_FILED : file_appeal()<br/>case frozen, Phase 2

    ISSUED --> [*]
    DENIED --> [*]
    WITHDRAWN --> [*]
    EXPIRED --> [*]
    APPEAL_FILED --> [*]
```

### State reference

| State | Clock | Who acts next | Notes |
|---|---|---|---|
| DRAFT | not started | applicant | Nothing is committed to the case record yet |
| SUBMITTED | running | system | Transient. Integration enrichment runs here |
| INTAKE_SCREENING | running | intake clerk | Completeness check against the derived checklist |
| RETURNED_INCOMPLETE | **paused** | applicant | Waiting on applicant, so the clock does not run |
| UNDER_REVIEW | running | discipline reviewers | Parallel tasks, one per routed discipline |
| REVISIONS_REQUESTED | **paused** | applicant | Waiting on applicant |
| PENDING_DECISION | running | supervisor | Every discipline has signed off |
| ISSUED / DENIED / WITHDRAWN / EXPIRED | stopped | none | Terminal |
| APPEAL_FILED | stopped | appeals board | Frozen. Phase 1 records only |

The paused states are the whole reason the SLA math is non-trivial. A case that sits for
three weeks waiting on an applicant has not consumed three weeks of the department's
20-day allowance, and reporting that it has would understate the department's actual
performance. See `docs/03-data-model.md` for how the pause intervals are stored.

## 2. Review task lifecycle

Each routed discipline gets its own task. Tasks are independent until every one of them
reaches a terminal outcome, at which point the parent application transitions.

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> ASSIGNED : assign()<br/>least-loaded certified reviewer
    ASSIGNED --> IN_PROGRESS : start()
    ASSIGNED --> PENDING : reassign()<br/>supervisor only, reason required
    IN_PROGRESS --> PENDING : reassign()

    IN_PROGRESS --> APPROVED : approve()
    IN_PROGRESS --> APPROVED_WITH_CONDITIONS : approve_with_conditions()<br/>guard: >=1 condition
    IN_PROGRESS --> DEFICIENT : record_deficiencies()<br/>guard: >=1 deficiency

    DEFICIENT --> PENDING : parent resubmitted<br/>new row at round N+1<br/>same reviewer retained if active

    APPROVED --> CANCELLED : parent withdrawn
    PENDING --> CANCELLED : parent withdrawn
    ASSIGNED --> CANCELLED : parent withdrawn
    IN_PROGRESS --> CANCELLED : parent withdrawn

    APPROVED --> [*]
    APPROVED_WITH_CONDITIONS --> [*]
    CANCELLED --> [*]
```

`DEFICIENT` is terminal for the round but not for the task. FR-12 requires that a
resubmission reopens only the disciplines that recorded deficiencies. A structural
reviewer who already approved does not review the case a second time because the fire
reviewer found a problem.

## 3. Swimlane view

```mermaid
flowchart TD
    subgraph APP[Applicant]
        A1[Prepare application]
        A2[Submit]
        A3[Correct and resubmit]
    end

    subgraph SYS[System]
        S1[Enrich from Property Records SOAP]
        S2[Verify license against Licensing DB]
        S3[AI extracts fields from narrative]
        S4[Derive required document checklist]
        S5[AI recommends discipline routing]
        S6[Assign least-loaded certified reviewer]
        S7[Start SLA clocks]
        S8[Raise escalation at 80% and at breach]
    end

    subgraph CLERK[Intake Clerk]
        C1{Complete?}
        C2[Return with itemized reasons]
        C3[Confirm or override AI fields]
        C4[Confirm or override routing]
    end

    subgraph REV[Discipline Reviewers]
        R1[Zoning review]
        R2[Structural review]
        R3[Fire review]
        R4[Environmental review]
        R5{All terminal?}
    end

    subgraph SUP[Supervisor]
        P1{Any deficiencies?}
        P2[Issue permit]
        P3[Deny with reason]
        P4[Monitor escalation queue]
    end

    A1 --> A2 --> S1 --> S2 --> S3 --> S4
    S4 --> C1
    C1 -- no --> C2 --> A3 --> C1
    C1 -- yes --> C3 --> S5 --> C4 --> S6 --> S7
    S6 --> R1 & R2 & R3 & R4
    R1 & R2 & R3 & R4 --> R5
    R5 -- no --> R5
    R5 -- yes --> P1
    P1 -- yes --> A3
    P1 -- no --> P2
    P1 -- no --> P3
    S7 --> S8 --> P4
```

## 4. Where the AI sits

Worth calling out separately, because the first question a client asks about an AI feature
is what happens when it is wrong.

```mermaid
flowchart LR
    N[Scope of work narrative] --> E[Field extraction]
    E --> CONF{Per-field<br/>confidence}
    CONF -- above threshold --> D[Pre-filled draft]
    CONF -- below threshold --> B[Left blank]
    D --> H[Intake clerk reviews]
    B --> H
    H --> COMMIT[(Committed to case)]
    H --> AUD[(Audit log:<br/>suggestion, decision, actor)]

    Q[Reviewer question] --> RET[Retrieve ordinance sections]
    RET --> G{Grounded in<br/>retrieved text?}
    G -- yes --> ANS[Answer with verbatim quote + citation]
    G -- no --> W[Withheld]
    ANS --> AUD
```

Both paths end at a human and at the audit log. Nothing the model produces moves the case
by itself, which is AI-04. The retrieval path withholds rather than guesses, which is
AI-06.

## 5. Escalation timing

SLA allowances are per permit type and per discipline, held in `sla_policies` rather than
in code so DPS can change them without a release.

```mermaid
gantt
    title Residential permit, 20 business day standard
    dateFormat X
    axisFormat %s

    section Application clock
    Intake screening (3d allowance)      :done, 0, 3
    Under review (14d allowance)         :active, 3, 17
    Pending decision (3d allowance)      :4, 17, 20

    section Escalations
    Warning at 80% of review allowance   :milestone, 14, 0
    Breach of review allowance           :crit, milestone, 17, 0
    Breach of council standard           :crit, milestone, 20, 0
```

Two escalation levels exist because a warning that fires at the same moment as the breach
is useless to a supervisor. The 80% warning is what gives them a chance to reassign.
