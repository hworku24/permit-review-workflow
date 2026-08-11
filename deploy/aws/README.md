# Deploying PermitFlow to AWS

A demonstration deployment. One Fargate task behind an application load balancer, with
CloudFront in front of it for HTTPS, and a small managed Postgres.

## What it costs

| Piece | Roughly |
|---|---|
| Application load balancer | $16 / month |
| Fargate, 0.5 vCPU and 1 GB, one task | $18 / month |
| RDS `db.t4g.micro`, 20 GB gp3 | $13 / month |
| CloudFront | free at this volume |
| ECR, Secrets Manager, CloudWatch Logs | under $1 / month |

About **$47 a month** while it is up, and nothing when it is torn down. `teardown.sh`
removes everything that bills.

## The shape of it

```
        browser
           |  HTTPS, CloudFront's certificate
      CloudFront
           |  HTTP, and the ALB security group only accepts
           |  CloudFront's published origin ranges
    application load balancer
           |  HTTP 8000, from the ALB security group only
     Fargate task
       api + soap-mock + licensing-verifier
           |  5432, from the task security group only
        RDS PostgreSQL
          not publicly accessible
```

Each hop is reachable only from the hop above it. The database security group has no CIDR
rule at all, only the task's security group, so there is no address on the internet that can
open a connection to it.

## Why CloudFront and not a certificate on the load balancer

ACM will not issue a certificate for `*.elb.amazonaws.com`, so an ALB cannot serve HTTPS
without a domain name somebody owns. CloudFront supplies a working certificate on its own
domain, which is the cheapest honest way to get HTTPS without buying a domain. The ALB then
only accepts traffic from CloudFront, so the plain HTTP origin is not a way around the front
door.

## Three containers in one task

`api` serves the screens and the API. `soap-mock` stands in for the county. `licensing-verifier`
is the Spring service that owns the JDBC connection to the licensing replica.

They share a network namespace, so `api` reaches the verifier on `localhost:8082` and nothing
outside the task can reach it at all. The verifier is the only container holding the replica
credential, which is the arrangement the split exists for: the credential the state issues
belongs to one service and not to every application that wants an answer.

The task went from 0.25 vCPU to 0.5 and from 0.5 GB to 1 GB when the JVM joined, which is
most of the difference in the monthly figure above.

## The sign-in

The deployment sets `DEMO_PASSPHRASE`, which puts one shared passphrase in front of every
screen. The passphrase is in Secrets Manager at `permitflow/demo-passphrase`.

It is a gate on a public address, not the city's single sign-on, and the sign-in page says
so. The user picker behind it still chooses which member of staff you are acting as, exactly
as it does locally, because the authorization being demonstrated is the engine's: every
action is checked against the actor's role no matter how the actor arrived.

## Running it

```bash
./deploy/aws/setup.sh      # stands everything up, prints the URL
./deploy/aws/refresh.sh    # builds, pushes, and rolls the service to the new image
./deploy/aws/teardown.sh   # removes everything that bills
```

`setup.sh` is safe to re-run. Every step checks whether the thing already exists.

## Things this deliberately does not do

**No multi-AZ, one task, one week of backups.** A demonstration that falls over for ten
minutes costs nothing. Paying for a standby to protect fictional permit data would be
spending money to look serious.

**No Kubernetes.** One container against one database. An orchestrator here would be a cost
and a complexity story with no benefit to point at.

**No autoscaling.** The load is one person clicking. A scaling policy that never fires is a
policy nobody has tested.

**Public subnets with a security group boundary, not private subnets with a NAT gateway.**
A NAT gateway is $32 a month, more than everything else here put together, and it would buy
a defence in depth that this data does not need. Worth knowing that this is the line where
a real deployment for real permit data would spend the money.
