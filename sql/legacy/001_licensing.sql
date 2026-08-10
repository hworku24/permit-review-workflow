-- State Department of Professional Regulation, contractor licensing replica
--
-- This runs in a SEPARATE database from PermitFlow. Rivermont has a read only account
-- against it and no ability to change the schema, which is why the column names below do
-- not match PermitFlow conventions and why there is no API in front of it. See
-- docs/04-integration-spec.md section I2.
--
-- The odd choices here are deliberate. A real legacy replica has char padding, a status
-- code column with no constraint, and a date column that is sometimes null. Writing the
-- integration against a clean table would skip the part of the work that is actually hard.

CREATE SCHEMA IF NOT EXISTS licensing;

CREATE TABLE licensing.contractor_license (
    license_number              char(12) PRIMARY KEY,
    business_name               varchar(120) NOT NULL,
    license_type                char(4) NOT NULL,
    status                      varchar(12) NOT NULL,
    issued_on                   date,
    expires_on                  date,
    disciplinary_action_count   integer DEFAULT 0,
    last_updated                timestamp DEFAULT now()
);

CREATE INDEX contractor_license_status_idx ON licensing.contractor_license(status);

-- A read only role, which is what the jurisdiction is actually granted.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'permitflow_ro') THEN
        CREATE ROLE permitflow_ro LOGIN PASSWORD 'readonly';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA licensing TO permitflow_ro;
GRANT SELECT ON licensing.contractor_license TO permitflow_ro;

-- ---------------------------------------------------------------------------
-- Sample licenses
-- Covers every branch of the rules table in the integration spec: active, expiring
-- soon, expired, suspended, revoked, and absent.
-- ---------------------------------------------------------------------------

INSERT INTO licensing.contractor_license
    (license_number, business_name, license_type, status, issued_on, expires_on, disciplinary_action_count)
VALUES
    ('VA-CL-004182', 'Harlowe Building Group LLC',   'CLSA', 'ACTIVE',    '2019-04-02', '2028-04-02', 0),
    ('VA-CL-007733', 'Pinecrest Construction Co',    'CLSA', 'ACTIVE',    '2021-11-15', '2027-11-15', 1),
    ('VA-CL-011290', 'Delmar Renovations Inc',       'CLSB', 'ACTIVE',    '2020-02-28', '2027-02-28', 0),
    ('VA-CL-013006', 'Southgate Commercial Builders','CLSA', 'ACTIVE',    '2018-07-09', '2029-07-09', 0),
    -- Expiring inside the 30 day warning window relative to the seeded case dates.
    ('VA-CL-016554', 'Ashby & Sons Contracting',     'CLSB', 'ACTIVE',    '2022-09-01', '2026-08-28', 0),
    ('VA-CL-018871', 'Quarry Lane Homes LLC',        'CLSC', 'EXPIRED',   '2017-05-20', '2025-05-20', 0),
    ('VA-CL-020445', 'Ridgeway Mechanical Corp',     'CLSB', 'SUSPENDED', '2016-03-14', '2027-03-14', 3),
    ('VA-CL-022108', 'Calvert Structures Inc',       'CLSA', 'REVOKED',   '2015-08-30', '2026-08-30', 5),
    ('VA-CL-025637', 'Foxglove Design Build',        'CLSB', 'ACTIVE',    '2023-01-17', '2028-01-17', 0),
    ('VA-CL-027914', 'Merrow Site Works LLC',        'CLSC', 'ACTIVE',    '2021-06-05', '2027-06-05', 0);
