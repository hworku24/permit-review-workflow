-- PermitFlow reporting views
-- Depends on 001_schema.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- v_application_summary
-- One row per application, joined out to the things a queue screen needs.
-- Answers P1: what is this application's status and who is it waiting on.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_application_summary AS
SELECT
    a.id                        AS application_id,
    a.application_number,
    a.status,
    a.status_since,
    pt.code                     AS permit_type_code,
    pt.name                     AS permit_type_name,
    pt.category                 AS permit_category,
    app.full_name               AS applicant_name,
    app.email                   AS applicant_email,
    c.business_name             AS contractor_name,
    c.license_status,
    p.apn,
    p.situs_address,
    p.zoning_code,
    a.declared_valuation,
    a.square_feet,
    a.submitted_at,
    a.intake_completed_at,
    a.decided_at,
    a.parcel_verified,
    a.license_verified,

    -- Who the department is waiting on. Drives the applicant-facing answer in US-02.
    CASE a.status
        WHEN 'DRAFT'                THEN 'applicant'
        WHEN 'SUBMITTED'            THEN 'system'
        WHEN 'INTAKE_SCREENING'     THEN 'intake clerk'
        WHEN 'RETURNED_INCOMPLETE'  THEN 'applicant'
        WHEN 'UNDER_REVIEW'         THEN 'discipline reviewers'
        WHEN 'REVISIONS_REQUESTED'  THEN 'applicant'
        WHEN 'PENDING_DECISION'     THEN 'supervisor'
        ELSE 'nobody'
    END                         AS waiting_on,

    -- True while the SLA clock is stopped for applicant wait time.
    EXISTS (
        SELECT 1 FROM clock_pause cp
        WHERE cp.application_id = a.id AND cp.resumed_at IS NULL
    )                           AS clock_paused,

    (SELECT COUNT(*) FROM review_task rt
      WHERE rt.application_id = a.id
        AND rt.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
    )                           AS open_task_count,

    (SELECT COUNT(*) FROM review_task rt
      WHERE rt.application_id = a.id AND rt.status = 'DEFICIENT'
    )                           AS deficient_task_count,

    (SELECT COUNT(*) FROM application_document ad
      WHERE ad.application_id = a.id AND ad.status = 'MISSING'
    )                           AS missing_document_count

FROM application a
JOIN permit_type pt ON pt.code = a.permit_type_code
JOIN applicant  app ON app.id  = a.applicant_id
JOIN parcel       p ON p.id    = a.parcel_id
LEFT JOIN contractor c ON c.id = a.contractor_id;

COMMENT ON VIEW v_application_summary IS
    'Operational queue view. One row per application with party, parcel, and workload counts.';

-- ---------------------------------------------------------------------------
-- v_sla_status
-- FR-14 and FR-16 at the application level, measured against the council standard.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_sla_status AS
SELECT
    a.id                        AS application_id,
    a.application_number,
    a.status,
    pt.code                     AS permit_type_code,
    pt.council_standard_days    AS allowance_days,
    application_net_business_days(a.id)     AS net_business_days_elapsed,
    pt.council_standard_days - application_net_business_days(a.id) AS days_remaining,

    CASE
        WHEN a.submitted_at IS NULL THEN 'NOT_STARTED'
        WHEN a.status IN ('ISSUED', 'DENIED', 'WITHDRAWN', 'EXPIRED', 'APPEAL_FILED') THEN
            CASE WHEN application_net_business_days(a.id) <= pt.council_standard_days
                 THEN 'MET' ELSE 'MISSED' END
        WHEN application_net_business_days(a.id) > pt.council_standard_days THEN 'BREACHED'
        WHEN application_net_business_days(a.id) >= (pt.council_standard_days * 0.80) THEN 'AT_RISK'
        ELSE 'ON_TRACK'
    END                         AS sla_state,

    EXISTS (
        SELECT 1 FROM clock_pause cp
        WHERE cp.application_id = a.id AND cp.resumed_at IS NULL
    )                           AS clock_paused

FROM application a
JOIN permit_type pt ON pt.code = a.permit_type_code;

COMMENT ON VIEW v_sla_status IS
    'Application-level SLA position, net of applicant wait time. Backs FR-14 and FR-23.';

-- ---------------------------------------------------------------------------
-- v_task_sla_status
-- The same question one level down. This is what the escalation job reads.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_task_sla_status AS
SELECT
    rt.id                       AS review_task_id,
    rt.application_id,
    a.application_number,
    rt.discipline_code,
    rt.round,
    rt.status,
    r.username                  AS reviewer_username,
    r.full_name                 AS reviewer_name,
    rt.assigned_at,
    rt.sla_due_at,
    rt.allowance_days,

    CASE WHEN rt.assigned_at IS NULL THEN NULL
         ELSE business_days_between(rt.assigned_at, COALESCE(rt.completed_at, now()))
    END                         AS business_days_open,

    CASE
        WHEN rt.status IN ('APPROVED', 'APPROVED_WITH_CONDITIONS', 'DEFICIENT', 'CANCELLED') THEN 'CLOSED'
        WHEN rt.assigned_at IS NULL THEN 'UNASSIGNED'
        WHEN business_days_between(rt.assigned_at, now()) > rt.allowance_days THEN 'BREACHED'
        WHEN business_days_between(rt.assigned_at, now()) >= (rt.allowance_days * 0.80) THEN 'AT_RISK'
        ELSE 'ON_TRACK'
    END                         AS sla_state

FROM review_task rt
JOIN application a ON a.id = rt.application_id
LEFT JOIN reviewer r ON r.id = rt.reviewer_id;

COMMENT ON VIEW v_task_sla_status IS
    'Per-task SLA position. Read by the escalation job to raise WARNING and BREACH rows.';

-- ---------------------------------------------------------------------------
-- v_reviewer_workload
-- FR-24 and pain point P4: assignment was uneven and nobody could see it.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_reviewer_workload AS
SELECT
    r.id                        AS reviewer_id,
    r.username,
    r.full_name,
    r.active,
    rd.discipline_code,

    COUNT(rt.id) FILTER (
        WHERE rt.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
    )                           AS open_tasks,

    COUNT(rt.id) FILTER (
        WHERE rt.status IN ('APPROVED', 'APPROVED_WITH_CONDITIONS', 'DEFICIENT')
          AND rt.completed_at >= now() - interval '30 days'
    )                           AS completed_last_30_days,

    MIN(rt.assigned_at) FILTER (
        WHERE rt.status IN ('ASSIGNED', 'IN_PROGRESS')
    )                           AS oldest_open_assignment,

    MAX(
        CASE WHEN rt.status IN ('ASSIGNED', 'IN_PROGRESS')
             THEN business_days_between(rt.assigned_at, now()) END
    )                           AS oldest_open_business_days

FROM reviewer r
JOIN reviewer_discipline rd ON rd.reviewer_id = r.id
LEFT JOIN review_task rt
       ON rt.reviewer_id = r.id
      AND rt.discipline_code = rd.discipline_code
WHERE r.role = 'reviewer'
GROUP BY r.id, r.username, r.full_name, r.active, rd.discipline_code;

COMMENT ON VIEW v_reviewer_workload IS
    'Open and recently completed work per reviewer per discipline. Backs assignment fairness.';

-- ---------------------------------------------------------------------------
-- v_cycle_time
-- FR-22. Net business days from submission to decision, per decided application.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_cycle_time AS
SELECT
    a.id                        AS application_id,
    a.application_number,
    a.permit_type_code,
    pt.name                     AS permit_type_name,
    pt.category                 AS permit_category,
    a.status,
    a.submitted_at,
    a.decided_at,
    date_trunc('month', a.decided_at)::date AS decided_month,

    business_days_between(a.submitted_at, a.decided_at)  AS gross_business_days,
    application_net_business_days(a.id, a.decided_at)    AS net_business_days,

    COALESCE((
        SELECT SUM(business_days_between(cp.paused_at, cp.resumed_at))
        FROM clock_pause cp
        WHERE cp.application_id = a.id AND cp.resumed_at IS NOT NULL
    ), 0)                       AS applicant_wait_business_days,

    (SELECT COUNT(DISTINCT rt.round) FROM review_task rt
      WHERE rt.application_id = a.id
    )                           AS review_rounds

FROM application a
JOIN permit_type pt ON pt.code = a.permit_type_code
WHERE a.decided_at IS NOT NULL
  AND a.submitted_at IS NOT NULL;

COMMENT ON VIEW v_cycle_time IS
    'Decided applications with gross, net, and applicant-wait durations. Gross minus wait equals net.';

-- ---------------------------------------------------------------------------
-- v_sla_compliance
-- FR-23. The number that goes to city council.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_sla_compliance AS
SELECT
    ct.decided_month,
    ct.permit_type_code,
    ct.permit_type_name,
    pt.council_standard_days,
    COUNT(*)                                                    AS decided_count,
    COUNT(*) FILTER (WHERE ct.net_business_days <= pt.council_standard_days) AS met_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE ct.net_business_days <= pt.council_standard_days)
        / NULLIF(COUNT(*), 0)
    , 1)                                                        AS compliance_pct,
    ROUND(AVG(ct.net_business_days), 1)                         AS mean_net_days,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ct.net_business_days)  AS median_net_days,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ct.net_business_days)  AS p90_net_days
FROM v_cycle_time ct
JOIN permit_type pt ON pt.code = ct.permit_type_code
WHERE ct.status = 'ISSUED' OR ct.status = 'DENIED'
GROUP BY ct.decided_month, ct.permit_type_code, ct.permit_type_name, pt.council_standard_days;

COMMENT ON VIEW v_sla_compliance IS
    'Monthly compliance against the council standard, with median and p90. Measured on net days.';

-- ---------------------------------------------------------------------------
-- v_open_escalations
-- FR-17. Supervisor queue, worst first.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_open_escalations AS
SELECT
    e.id                        AS escalation_id,
    e.level,
    e.reason,
    e.raised_at,
    a.id                        AS application_id,
    a.application_number,
    a.status                    AS application_status,
    pt.name                     AS permit_type_name,
    rt.id                       AS review_task_id,
    rt.discipline_code,
    r.full_name                 AS reviewer_name,

    -- Ordering key: how far past the allowance this task is. Unassigned tasks have no
    -- allowance to be past, so they sort by age instead.
    COALESCE(
        business_days_between(rt.assigned_at, now()) - rt.allowance_days,
        business_days_between(a.status_since, now())
    )                           AS business_days_past_due

FROM escalation e
JOIN application a ON a.id = e.application_id
JOIN permit_type pt ON pt.code = a.permit_type_code
LEFT JOIN review_task rt ON rt.id = e.review_task_id
LEFT JOIN reviewer r ON r.id = rt.reviewer_id
WHERE e.acknowledged_at IS NULL
ORDER BY
    CASE e.level WHEN 'BREACH' THEN 0 ELSE 1 END,
    business_days_past_due DESC NULLS LAST,
    e.raised_at;

COMMENT ON VIEW v_open_escalations IS
    'Unacknowledged escalations, breaches first, then by how far past due. Backs FR-17.';

-- ---------------------------------------------------------------------------
-- v_discipline_bottleneck
-- FR-24 and pain point P4 from the other direction. v_reviewer_workload answers
-- "who is loaded"; this answers "where do cases sit". A supervisor deciding whether
-- to move a reviewer needs the second question, and one reviewer being busy is not
-- the same fact as one discipline being the reason cases are late.
--
-- Every configured discipline appears, including ones with no work, because a
-- discipline missing from the list reads as zero and a discipline showing zero reads
-- as measured.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_discipline_bottleneck AS
SELECT
    d.code                      AS discipline_code,
    d.name                      AS discipline_name,
    d.active,

    count(t.review_task_id) FILTER (
        WHERE t.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
    )                           AS open_tasks,

    count(t.review_task_id) FILTER (WHERE t.sla_state = 'BREACHED')  AS breached_tasks,
    count(t.review_task_id) FILTER (WHERE t.sla_state = 'AT_RISK')   AS at_risk_tasks,
    count(t.review_task_id) FILTER (WHERE t.sla_state = 'UNASSIGNED') AS unassigned_tasks,

    max(t.business_days_open) FILTER (
        WHERE t.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
    )                           AS oldest_open_business_days,

    round(avg(t.business_days_open) FILTER (
        WHERE t.status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
    ), 1)                       AS mean_open_business_days,

    count(t.review_task_id) FILTER (
        WHERE rt.completed_at > now() - interval '30 days'
    )                           AS completed_last_30_days,

    -- Turnaround on recently closed work, which is the number that says whether a
    -- queue is deep because the discipline is slow or because the work arrived.
    round(avg(business_days_between(rt.assigned_at, rt.completed_at)) FILTER (
        WHERE rt.completed_at > now() - interval '30 days' AND rt.assigned_at IS NOT NULL
    ), 1)                       AS mean_business_days_to_complete

FROM discipline d
LEFT JOIN v_task_sla_status t ON t.discipline_code = d.code
LEFT JOIN review_task rt ON rt.id = t.review_task_id
GROUP BY d.code, d.name, d.active;

COMMENT ON VIEW v_discipline_bottleneck IS
    'Open work, overdue work, and recent turnaround per discipline. Answers where cases sit.';

COMMIT;
