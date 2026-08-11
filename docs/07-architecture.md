# Architecture

Two views. The first is what the code does. The second is what is deployed. They are drawn
separately because they answer different questions, and a single diagram that tries to do
both ends up showing neither clearly.

Both are Mermaid, so they are text in the repository and GitHub renders them. A picture that
lives as a PNG somewhere goes out of date the first time somebody changes the thing it
describes, and nobody notices.

## 1. The application

```mermaid
flowchart TB
    subgraph people[" "]
        direction LR
        clerk["Intake clerk"]
        reviewer["Discipline reviewer"]
        supervisor["Supervisor"]
    end

    subgraph app["FastAPI application, one process"]
        direction TB
        ui["Staff screens<br/>queue, case, triage,<br/>ordinance, dashboard, admin"]
        api["JSON API<br/>applications, tasks, queues,<br/>reports, ai"]
        engine["Process engine<br/>state machine, guards, SLA clock,<br/>assignment, routing, escalation"]
        ai["AI layer<br/>BM25 retrieval, grounded answers,<br/>intake triage, provider seam"]
        integ["Integration layer<br/>retry, circuit breaker,<br/>attempt log"]
    end

    db[("PostgreSQL<br/>24 tables, 8 views,<br/>append-only audit")]
    corpus[/"Zoning ordinance<br/>35 sections, on disk"/]
    county["County property records<br/>SOAP 1.1 over HTTP"]
    state["State licensing replica<br/>direct read-only connection"]

    clerk --> ui
    reviewer --> ui
    supervisor --> ui
    ui --> engine
    api --> engine
    ui --> ai
    api --> ai
    engine --> db
    engine --> integ
    ai --> corpus
    ai -. "recommendations only,<br/>never a status change" .-> db
    integ --> county
    integ --> state
    ui -. "reads reporting views directly" .-> db

    classDef external fill:#fdf3e3,stroke:#8a5200,color:#8a5200
    classDef store fill:#e7edf4,stroke:#1f4e79,color:#1f4e79
    class county,state external
    class db,corpus store
```

Three things in that picture are worth stating out loud.

**The engine is the only thing that moves a case.** Both the screens and the API call it, and
neither writes a status column. Every transition is checked against the state machine and
the actor's role before it happens.

**The AI layer has no path that changes a status.** It writes recommendations and reads the
corpus. A named human accepts or overrides, and that decision is what moves anything.

**The screens read the reporting views directly** for anything they only display. That is the
dotted line on the right. It is deliberate: the compliance number goes to city council, and
recomputing it in Python next to the SQL that already computes it is how two versions of one
number start to disagree.

## 2. The deployment

```mermaid
flowchart TB
    browser["Browser"]

    subgraph aws["AWS, us-east-1"]
        direction TB
        cf["CloudFront<br/>TLS, its own certificate"]

        subgraph vpc["Default VPC"]
            direction TB
            alb["Application load balancer<br/>HTTP :80"]

            subgraph task["Fargate task, 0.25 vCPU"]
                direction LR
                capi["api<br/>:8000"]
                csoap["soap-mock<br/>:8081"]
            end

            rds[("RDS PostgreSQL<br/>db.t4g.micro, not publicly accessible")]
        end

        ecr["ECR"]
        sm["Secrets Manager<br/>db url, passphrase,<br/>session secret"]
        logs["CloudWatch Logs"]
    end

    browser -- "HTTPS" --> cf
    cf -- "HTTP, origin ranges only" --> alb
    alb -- ":8000, from the ALB group only" --> capi
    capi -- ":5432, from the task group only" --> rds
    capi -- "SOAP" --> csoap
    ecr -. "image pulled at start" .-> task
    sm -. "injected at start" .-> task
    task -. "stdout" .-> logs

    classDef edge fill:#eaf5ee,stroke:#1f5c34,color:#1f5c34
    classDef store fill:#e7edf4,stroke:#1f4e79,color:#1f4e79
    class cf,alb edge
    class rds,ecr,sm,logs store
```

Each hop accepts traffic only from the hop above it, enforced by security groups and not
by convention:

| Hop | Accepts from |
|---|---|
| CloudFront | the internet, HTTPS only |
| Load balancer | CloudFront's published origin ranges, and nothing else |
| Fargate task | the load balancer's security group |
| Database | the task's security group, with no CIDR rule at all |

The last row is the one worth checking on the console. There is no address on the internet
that can open a connection to the database, because no address appears in its rules.

## 3. What is not in either picture

**`licensing-verifier/`, the Spring service, is not in the request path.** It holds the same
verification rules over a JDBC datasource, it has its own tests, and CI runs them. Nothing
calls it. The seam it would plug into exists (`LicensingBackend` in
`permitflow/integrations/licensing.py`) and has one implementation, the direct connection
the application uses today.

It is drawn nowhere above because drawing it would say something untrue about how a request
flows. Wiring it in means an HTTP backend behind that seam, a parity test driving both
implementations over the same replica rows, and a third container in the task definition.

**The applicant portal.** Applicants are external and have no screen here. Phase 1 is intake
through issuance for department staff, and `docs/01-requirements.md` says so.

**Inspections, fees, certificates of occupancy, appeals hearings.** Out of scope, listed as
such in the requirements, and not started.
