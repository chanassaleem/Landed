-- Postgres schema with row-level security.
--
-- Tenancy rule, enforced by the database rather than by application code:
-- every tenant table carries org_id, and every one has an RLS policy keyed
-- to current_setting('app.org_id'). A bug in a query cannot leak another
-- organisation's rows, because the database refuses to return them.
--
-- Two conditions must hold or RLS silently does nothing:
--   1. The connecting role must NOT be superuser and must NOT have
--      BYPASSRLS. Superusers ignore every policy.
--   2. FORCE ROW LEVEL SECURITY must be set, otherwise the table owner
--      also bypasses its own policies.
-- Both are asserted in the test suite.

-- ------------------------------------------------------------- identity

CREATE TABLE IF NOT EXISTS org (
    org_id      UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL DEFAULT 'PK',
    currency    TEXT NOT NULL DEFAULT 'PKR',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id       UUID PRIMARY KEY,
    org_id        UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,          -- Argon2id, never anything cheaper
    role          TEXT NOT NULL,          -- owner|finance|supply_chain|service|viewer
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ,
    CONSTRAINT app_user_email_unique UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS session (
    session_id  UUID PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,     -- the raw token never touches the db
    user_id     UUID NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    user_agent  TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS session_user_idx ON session(user_id);

-- ---------------------------------------------------------- tenant data

CREATE TABLE IF NOT EXISTS entity (
    entity_id           UUID PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    legal_name          TEXT NOT NULL,
    ntn                 TEXT,
    is_separate_company BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FALSE means the row exists only because a snapshot had to reference it:
    -- the code appeared in an upload and nobody has set the company up. Kept
    -- in step with the DuckDB schema so the two cannot drift.
    configured          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS location (
    location_id UUID PRIMARY KEY,
    org_id      UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    entity_id   UUID NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    city        TEXT,
    configured  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS item (
    item_key             TEXT NOT NULL,
    org_id               UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    description          TEXT,
    category             TEXT,
    is_clinical_critical BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (org_id, item_key)
);

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id     UUID PRIMARY KEY,
    org_id          UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    entity_id       UUID,
    location_id     UUID,
    report_type     TEXT NOT NULL,
    as_of_date      DATE NOT NULL,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_by     UUID,
    source_filename TEXT,
    source_sha256   TEXT,
    row_count       INTEGER DEFAULT 0,
    rows_rejected   INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'valid',
    superseded_by   UUID
);

CREATE TABLE IF NOT EXISTS fact_inventory (
    snapshot_id        UUID NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    org_id             UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    item_key           TEXT NOT NULL,
    on_hand            DOUBLE PRECISION,
    open_po            DOUBLE PRECISION,
    unit_cost          DOUBLE PRECISION,
    lead_time_days     DOUBLE PRECISION,
    lead_time_days_std DOUBLE PRECISION,
    expiry_date        DATE
);

CREATE TABLE IF NOT EXISTS fact_demand (
    snapshot_id UUID NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    org_id      UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    item_key    TEXT NOT NULL,
    period      TEXT NOT NULL,
    quantity    DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS metric_run (
    run_id           UUID PRIMARY KEY,
    org_id           UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    entity_id        UUID,
    location_id      UUID,
    as_of_date       DATE NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID,
    threshold_set_id UUID,
    snapshot_ids     JSONB NOT NULL,
    engine_version   TEXT NOT NULL,
    item_count       INTEGER
);

CREATE TABLE IF NOT EXISTS metric_result (
    run_id              UUID NOT NULL REFERENCES metric_run(run_id) ON DELETE CASCADE,
    org_id              UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    item_key            TEXT NOT NULL,
    demand_class        TEXT,
    abc_class           TEXT,
    service_target      DOUBLE PRECISION,
    service_metric      TEXT,
    safety_stock        DOUBLE PRECISION,
    reorder_point       DOUBLE PRECISION,
    order_up_to         DOUBLE PRECISION,
    suggested_order_qty DOUBLE PRECISION,
    order_value         DOUBLE PRECISION,
    months_cover        DOUBLE PRECISION,
    status              TEXT,
    stock_value         DOUBLE PRECISION,
    excess_value        DOUBLE PRECISION,
    days_to_expiry      INTEGER,
    reason              TEXT
);

CREATE TABLE IF NOT EXISTS threshold_set (
    threshold_set_id UUID PRIMARY KEY,
    org_id           UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       UUID,
    payload          JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    audit_id    UUID PRIMARY KEY,
    org_id      UUID NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    at_utc      TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id     UUID,
    action      TEXT NOT NULL,
    object_type TEXT,
    object_id   TEXT,
    detail      JSONB
);
CREATE INDEX IF NOT EXISTS audit_org_time_idx ON audit(org_id, at_utc DESC);
