-- PermitFlow schema
-- City of Rivermont, Department of Permitting Services
--
-- Run order: 001_schema.sql, 002_views.sql, 003_seed.sql
--
-- All timestamps are timestamptz. The database timezone is set to America/New_York in
-- docker-compose.yml because business day math casts timestamps to dates, and the
-- department's business day is a local calendar day.

BEGIN;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

CREATE TABLE zoning_district (
    code            text PRIMARY KEY,
    name            text NOT NULL,
    category        text NOT NULL CHECK (category IN ('residential', 'commercial', 'industrial', 'mixed', 'special')),
    description     text
);

CREATE TABLE permit_type (
    code            text PRIMARY KEY,
    name            text NOT NULL,
    category        text NOT NULL CHECK (category IN ('residential', 'commercial')),
    -- The council standard for a first decision, in business days. FR-23 reports against this.
    council_standard_days integer NOT NULL CHECK (council_standard_days > 0),
    active          boolean NOT NULL DEFAULT true
);

CREATE TABLE discipline (
    code            text PRIMARY KEY,
    name            text NOT NULL,
    description     text,
    active          boolean NOT NULL DEFAULT true
);

CREATE TABLE document_type (
    code            text PRIMARY KEY,
    name            text NOT NULL,
    description     text
);

-- Which documents a permit type requires. FR-04 derives the checklist from these rows,
-- which is why adding a requirement is configuration rather than a release.
CREATE TABLE permit_type_document (
    permit_type_code    text NOT NULL REFERENCES permit_type(code),
    document_type_code  text NOT NULL REFERENCES document_type(code),
    required            boolean NOT NULL DEFAULT true,
    -- Optional gate: only required when declared valuation is at or above this amount.
    min_valuation       numeric(12,2),
    PRIMARY KEY (permit_type_code, document_type_code)
);

-- Base routing rules. FR-07 always routes these; the AI recommends additions on top.
CREATE TABLE permit_type_discipline (
    permit_type_code    text NOT NULL REFERENCES permit_type(code),
    discipline_code     text NOT NULL REFERENCES discipline(code),
    always_required     boolean NOT NULL DEFAULT true,
    PRIMARY KEY (permit_type_code, discipline_code)
);

-- SLA allowances per permit type and lifecycle phase. Held as data so DPS can change an
-- allowance without a deployment, per the note in 03-data-model.
CREATE TABLE sla_policy (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    permit_type_code    text NOT NULL REFERENCES permit_type(code),
    phase               text NOT NULL CHECK (phase IN ('INTAKE_SCREENING', 'REVIEW_TASK', 'PENDING_DECISION')),
    -- Null discipline means the policy applies to every discipline in that phase.
    discipline_code     text REFERENCES discipline(code),
    allowance_days      integer NOT NULL CHECK (allowance_days > 0),
    warning_threshold   numeric(3,2) NOT NULL DEFAULT 0.80
                            CHECK (warning_threshold > 0 AND warning_threshold < 1),
    UNIQUE (permit_type_code, phase, discipline_code)
);

CREATE TABLE holiday (
    holiday_date    date PRIMARY KEY,
    name            text NOT NULL
);

-- ---------------------------------------------------------------------------
-- Parties and property
-- ---------------------------------------------------------------------------

CREATE TABLE applicant (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name       text NOT NULL,
    email           text NOT NULL,
    phone           text,
    mailing_address text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contractor (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    license_number          text NOT NULL UNIQUE,
    business_name           text NOT NULL,
    -- Mirrored from the state licensing database at verification time. UNVERIFIED is a
    -- distinct value from EXPIRED on purpose: an outage must never read as a pass.
    license_status          text NOT NULL DEFAULT 'UNVERIFIED'
                                CHECK (license_status IN ('ACTIVE', 'EXPIRED', 'SUSPENDED', 'REVOKED', 'NOT_FOUND', 'UNVERIFIED')),
    license_expires_on      date,
    last_verified_at        timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE parcel (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    apn                 text NOT NULL UNIQUE,
    situs_address       text NOT NULL,
    zoning_code         text REFERENCES zoning_district(code),
    owner_name          text,
    acreage             numeric(8,3),
    stop_work_order     boolean NOT NULL DEFAULT false,
    -- Null means the county lookup has never succeeded for this parcel.
    last_synced_at      timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reviewer (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username        text NOT NULL UNIQUE,
    full_name       text NOT NULL,
    email           text NOT NULL,
    role            text NOT NULL CHECK (role IN ('intake_clerk', 'reviewer', 'supervisor')),
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reviewer_discipline (
    reviewer_id     uuid NOT NULL REFERENCES reviewer(id) ON DELETE CASCADE,
    discipline_code text NOT NULL REFERENCES discipline(code),
    certified_on    date NOT NULL,
    PRIMARY KEY (reviewer_id, discipline_code)
);

-- ---------------------------------------------------------------------------
-- The case
-- ---------------------------------------------------------------------------

CREATE SEQUENCE application_number_seq;

CREATE TABLE application (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_number      text NOT NULL UNIQUE,
    applicant_id            uuid NOT NULL REFERENCES applicant(id),
    contractor_id           uuid REFERENCES contractor(id),
    parcel_id               uuid NOT NULL REFERENCES parcel(id),
    permit_type_code        text NOT NULL REFERENCES permit_type(code),

    status                  text NOT NULL CHECK (status IN (
                                'DRAFT', 'SUBMITTED', 'INTAKE_SCREENING', 'RETURNED_INCOMPLETE',
                                'UNDER_REVIEW', 'REVISIONS_REQUESTED', 'PENDING_DECISION',
                                'ISSUED', 'DENIED', 'WITHDRAWN', 'EXPIRED', 'APPEAL_FILED')),
    -- Denormalized from status_history. See 03-data-model section 2 for why.
    status_since            timestamptz NOT NULL DEFAULT now(),

    scope_narrative         text NOT NULL,
    declared_valuation      numeric(12,2) NOT NULL CHECK (declared_valuation >= 0),
    square_feet             integer CHECK (square_feet IS NULL OR square_feet > 0),
    occupancy_class         text,

    -- The SLA clock starts here, not at row creation. A draft consumes no allowance.
    submitted_at            timestamptz,
    intake_completed_at     timestamptz,
    decided_at              timestamptz,

    -- Set when the county lookup could not be completed, so the gap is visible per NFR-02.
    parcel_verified         boolean NOT NULL DEFAULT false,
    license_verified        boolean NOT NULL DEFAULT false,

    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              text NOT NULL,

    CONSTRAINT submitted_before_intake CHECK (
        intake_completed_at IS NULL OR submitted_at IS NULL OR intake_completed_at >= submitted_at),
    -- APPEAL_FILED is included because it is only reachable from DENIED, which means the
    -- decision timestamp is already set by the time an appeal freezes the case.
    CONSTRAINT decided_states_have_decided_at CHECK (
        (status IN ('ISSUED', 'DENIED', 'APPEAL_FILED')) = (decided_at IS NOT NULL))
);

CREATE INDEX application_status_idx ON application(status);
CREATE INDEX application_parcel_idx ON application(parcel_id);
CREATE INDEX application_permit_type_idx ON application(permit_type_code);

CREATE TABLE application_document (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id      uuid NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    document_type_code  text NOT NULL REFERENCES document_type(code),
    status              text NOT NULL CHECK (status IN ('MISSING', 'RECEIVED', 'WAIVED', 'SUPERSEDED')),
    filename            text,
    uploaded_at         timestamptz,
    uploaded_by         text,
    -- A waiver is a decision someone made, so it needs an owner and a reason.
    waived_by           text,
    waiver_reason       text,
    CONSTRAINT waiver_has_reason CHECK (
        status <> 'WAIVED' OR (waived_by IS NOT NULL AND waiver_reason IS NOT NULL)),
    CONSTRAINT received_has_file CHECK (
        status <> 'RECEIVED' OR filename IS NOT NULL)
);

CREATE UNIQUE INDEX application_document_current_idx
    ON application_document(application_id, document_type_code)
    WHERE status <> 'SUPERSEDED';

-- ---------------------------------------------------------------------------
-- Review
-- ---------------------------------------------------------------------------

CREATE TABLE review_task (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id      uuid NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    discipline_code     text NOT NULL REFERENCES discipline(code),
    -- FR-12: a resubmission opens a new round rather than reusing the old task.
    round               integer NOT NULL DEFAULT 1 CHECK (round > 0),
    reviewer_id         uuid REFERENCES reviewer(id),

    status              text NOT NULL CHECK (status IN (
                            'PENDING', 'ASSIGNED', 'IN_PROGRESS',
                            'APPROVED', 'APPROVED_WITH_CONDITIONS', 'DEFICIENT', 'CANCELLED')),

    assigned_at         timestamptz,
    started_at          timestamptz,
    completed_at        timestamptz,
    sla_due_at          timestamptz,
    allowance_days      integer,

    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (application_id, discipline_code, round),
    CONSTRAINT assigned_has_reviewer CHECK (
        status IN ('PENDING', 'CANCELLED') OR reviewer_id IS NOT NULL),
    CONSTRAINT terminal_has_completed_at CHECK (
        (status IN ('APPROVED', 'APPROVED_WITH_CONDITIONS', 'DEFICIENT', 'CANCELLED'))
        = (completed_at IS NOT NULL))
);

CREATE INDEX review_task_application_idx ON review_task(application_id);
CREATE INDEX review_task_open_idx ON review_task(reviewer_id, status)
    WHERE status IN ('PENDING', 'ASSIGNED', 'IN_PROGRESS');

CREATE TABLE deficiency (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_task_id      uuid NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    -- FR-19 requires a denial to reference a code provision, so deficiencies carry one.
    code_reference      text NOT NULL,
    description         text NOT NULL,
    severity            text NOT NULL CHECK (severity IN ('MAJOR', 'MINOR')),
    resolved_at         timestamptz,
    resolved_in_round   integer,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL
);

CREATE INDEX deficiency_task_idx ON deficiency(review_task_id);

CREATE TABLE review_condition (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_task_id      uuid NOT NULL REFERENCES review_task(id) ON DELETE CASCADE,
    description         text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL
);

-- ---------------------------------------------------------------------------
-- Clock, history, escalation
-- ---------------------------------------------------------------------------

CREATE TABLE status_history (
    id                  bigserial PRIMARY KEY,
    application_id      uuid NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    from_status         text,
    to_status           text NOT NULL,
    actor               text NOT NULL,
    reason              text,
    occurred_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX status_history_application_idx ON status_history(application_id, occurred_at);

CREATE TABLE clock_pause (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id      uuid NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    paused_at           timestamptz NOT NULL,
    resumed_at          timestamptz,
    reason              text NOT NULL,
    CONSTRAINT pause_ordering CHECK (resumed_at IS NULL OR resumed_at >= paused_at)
);

-- An application cannot be paused twice at once.
CREATE UNIQUE INDEX clock_pause_one_open_idx
    ON clock_pause(application_id) WHERE resumed_at IS NULL;

CREATE TABLE escalation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id      uuid NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    review_task_id      uuid REFERENCES review_task(id) ON DELETE CASCADE,
    level               text NOT NULL CHECK (level IN ('WARNING', 'BREACH')),
    reason              text NOT NULL,
    raised_at           timestamptz NOT NULL DEFAULT now(),
    acknowledged_at     timestamptz,
    acknowledged_by     text
);

-- FR-16: one escalation per level per task, so a scheduler that runs every five minutes
-- does not produce 288 identical rows a day.
CREATE UNIQUE INDEX escalation_task_level_idx
    ON escalation(review_task_id, level) WHERE review_task_id IS NOT NULL;
CREATE UNIQUE INDEX escalation_application_level_idx
    ON escalation(application_id, level) WHERE review_task_id IS NULL;

-- ---------------------------------------------------------------------------
-- Integrations and AI
-- ---------------------------------------------------------------------------

CREATE TABLE integration_call (
    id                  bigserial PRIMARY KEY,
    -- Deliberately NOT a foreign key. This table is written from a separate autocommit
    -- connection so an attempt log survives the rollback of the transaction that made the
    -- call. A foreign key here would make that write take a FOR KEY SHARE lock on the
    -- application row, which conflicts with the FOR UPDATE the calling transaction is
    -- already holding. The result is a hang that Postgres cannot resolve as a deadlock,
    -- because one side is waiting in the database and the other in application code.
    application_id      uuid,
    system              text NOT NULL CHECK (system IN ('PROPERTY_RECORDS', 'BUSINESS_LICENSING')),
    operation           text NOT NULL,
    attempt             integer NOT NULL DEFAULT 1,
    status              text NOT NULL CHECK (status IN ('SUCCESS', 'FAULT', 'TIMEOUT', 'ERROR', 'CIRCUIT_OPEN')),
    request_payload     jsonb,
    response_payload    jsonb,
    error_detail        text,
    latency_ms          integer,
    attempted_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX integration_call_application_idx ON integration_call(application_id);

CREATE TABLE ai_recommendation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id      uuid REFERENCES application(id) ON DELETE CASCADE,
    kind                text NOT NULL CHECK (kind IN ('field_extraction', 'discipline_routing', 'ordinance_answer')),
    model               text NOT NULL,
    prompt_version      text NOT NULL,
    payload             jsonb NOT NULL,
    confidence          numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    sources             jsonb,
    -- Null until a human decides. AI-04 means this stays null rather than defaulting true.
    accepted            boolean,
    override_payload    jsonb,
    decided_by          text,
    decided_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT decision_is_complete CHECK (
        (accepted IS NULL) = (decided_at IS NULL)
        AND (accepted IS NULL) = (decided_by IS NULL))
);

CREATE INDEX ai_recommendation_application_idx ON ai_recommendation(application_id, kind);

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    id                  bigserial PRIMARY KEY,
    entity_type         text NOT NULL CHECK (entity_type IN (
                            'application', 'review_task', 'document', 'integration', 'ai', 'escalation')),
    entity_id           uuid,
    action              text NOT NULL,
    actor               text NOT NULL,
    before_value        jsonb,
    after_value         jsonb,
    occurred_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_entity_idx ON audit_log(entity_type, entity_id, occurred_at);

-- NFR-04 and FR-21. Enforced here rather than in application code, because a convention
-- the application follows is a convention the next developer can break.
CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();

-- TRUNCATE bypasses row-level triggers, so it needs its own statement-level guard.
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();

-- ---------------------------------------------------------------------------
-- Business day arithmetic
-- ---------------------------------------------------------------------------

-- Counts business days in the half-open interval [start, end). Same day is zero.
-- Mirrored in permitflow/process/sla.py. tests/test_sla_parity.py asserts the two agree.
CREATE OR REPLACE FUNCTION business_days_between(p_start timestamptz, p_end timestamptz)
RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(COUNT(*), 0)::integer
    FROM generate_series(p_start::date, (p_end::date - 1), interval '1 day') AS g(day)
    WHERE EXTRACT(ISODOW FROM g.day) < 6
      AND NOT EXISTS (
          SELECT 1 FROM holiday h WHERE h.holiday_date = g.day::date
      );
$$;

-- Adds a number of business days to a timestamp, landing on the end of that business day.
CREATE OR REPLACE FUNCTION add_business_days(p_start timestamptz, p_days integer)
RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
DECLARE
    cursor_date date := p_start::date;
    remaining   integer := p_days;
BEGIN
    WHILE remaining > 0 LOOP
        cursor_date := cursor_date + 1;
        IF EXTRACT(ISODOW FROM cursor_date) < 6
           AND NOT EXISTS (SELECT 1 FROM holiday h WHERE h.holiday_date = cursor_date) THEN
            remaining := remaining - 1;
        END IF;
    END LOOP;
    RETURN (cursor_date + time '17:00')::timestamptz;
END;
$$;

-- Business days an application has consumed: elapsed since submission, minus time spent
-- waiting on the applicant. FR-14 and FR-15.
CREATE OR REPLACE FUNCTION application_net_business_days(p_application_id uuid, p_as_of timestamptz DEFAULT now())
RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT GREATEST(
        0,
        business_days_between(a.submitted_at, LEAST(p_as_of, COALESCE(a.decided_at, p_as_of)))
        - COALESCE((
            SELECT SUM(business_days_between(cp.paused_at, COALESCE(cp.resumed_at, p_as_of)))
            FROM clock_pause cp
            WHERE cp.application_id = a.id
        ), 0)
    )::integer
    FROM application a
    WHERE a.id = p_application_id
      AND a.submitted_at IS NOT NULL;
$$;

COMMIT;
