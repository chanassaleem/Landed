-- S&OP Insight: canonical schema.
--
-- Two rules shape everything here:
--   1. Facts are immutable. An uploaded report is stored whole, as a dated
--      snapshot, and never edited. A correction is a NEW snapshot that
--      supersedes the old one; the old one stays for audit.
--   2. Entity and location are carried on every row from the start, so the
--      multi-entity group view needs no migration later.
--
-- Computed metrics are cached, never treated as truth. Each metric_run
-- records which snapshots and which threshold set produced it, so any past
-- dashboard can be explained eighteen months later.

-- ---------------------------------------------------------------- master

CREATE TABLE IF NOT EXISTS entity (
    entity_id            VARCHAR PRIMARY KEY,
    legal_name           VARCHAR NOT NULL,
    ntn                  VARCHAR,
    is_separate_company  BOOLEAN NOT NULL DEFAULT TRUE,
    base_currency        VARCHAR NOT NULL DEFAULT 'PKR',
    created_at           TIMESTAMP NOT NULL,
    -- FALSE means nobody set this up: the code simply appeared in an
    -- uploaded file and a row had to exist for the snapshot to reference.
    -- Settings keeps the two apart so a guess is never shown as a decision.
    configured           BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS location (
    location_id   VARCHAR PRIMARY KEY,
    entity_id     VARCHAR NOT NULL,
    name          VARCHAR NOT NULL,
    city          VARCHAR,
    is_warehouse  BOOLEAN NOT NULL DEFAULT TRUE,
    configured    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS item (
    item_key             VARCHAR PRIMARY KEY,
    canonical_code       VARCHAR,
    description          VARCHAR,
    category             VARCHAR,
    uom                  VARCHAR DEFAULT 'unit',
    pack_size            DOUBLE DEFAULT 1,
    shelf_life_days      INTEGER,
    is_consumable        BOOLEAN DEFAULT TRUE,
    is_clinical_critical BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS customer (
    customer_key           VARCHAR PRIMARY KEY,
    canonical_name         VARCHAR NOT NULL,
    sector                 VARCHAR,          -- public | private
    is_intercompany        BOOLEAN DEFAULT FALSE,
    intercompany_entity_id VARCHAR,          -- which of our entities it is
    credit_terms_days      INTEGER
);

CREATE TABLE IF NOT EXISTS machine (
    machine_key   VARCHAR PRIMARY KEY,
    model         VARCHAR,
    customer_key  VARCHAR,
    location_id   VARCHAR,
    install_date  DATE,
    contract_type VARCHAR          -- sale | reagent_rental | loan
);

-- One hospital arrives as four different spellings across three ERPs.
-- Until those resolve to one key, consolidated figures are wrong and look
-- plausible, which is worse than being obviously wrong.
CREATE TABLE IF NOT EXISTS alias (
    alias_id         VARCHAR PRIMARY KEY,
    target_type      VARCHAR NOT NULL,   -- item | customer | machine
    target_key       VARCHAR,            -- NULL while unresolved
    raw_value        VARCHAR NOT NULL,
    source_entity_id VARCHAR,
    confidence       DOUBLE,
    resolved_by      VARCHAR,            -- auto | user | unresolved
    resolved_at      TIMESTAMP
);

-- ------------------------------------------------------------ ingestion

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id     VARCHAR PRIMARY KEY,
    entity_id       VARCHAR NOT NULL,
    location_id     VARCHAR NOT NULL,
    report_type     VARCHAR NOT NULL,   -- inventory | demand | receivables | service | placements
    as_of_date      DATE NOT NULL,      -- the date the DATA describes
    uploaded_at     TIMESTAMP NOT NULL, -- when it arrived, a different thing
    uploaded_by     VARCHAR,
    source_filename VARCHAR,
    source_sha256   VARCHAR,            -- same file twice is detected, not duplicated
    source_bytes    BIGINT,
    mapping_id      VARCHAR,
    row_count       INTEGER,
    rows_rejected   INTEGER DEFAULT 0,
    status          VARCHAR NOT NULL,   -- valid | partial | rejected
    superseded_by   VARCHAR             -- set when a re-upload replaces it
);

CREATE TABLE IF NOT EXISTS fact_inventory (
    snapshot_id        VARCHAR NOT NULL,
    item_key           VARCHAR NOT NULL,
    on_hand            DOUBLE,
    open_po            DOUBLE,
    unit_cost          DOUBLE,
    lead_time_days     DOUBLE,
    lead_time_days_std DOUBLE,
    expiry_date        DATE
);

CREATE TABLE IF NOT EXISTS fact_demand (
    snapshot_id VARCHAR NOT NULL,
    item_key    VARCHAR NOT NULL,
    period      VARCHAR NOT NULL,   -- YYYY-MM
    quantity    DOUBLE
);

-- Rows that failed validation are quarantined, never silently dropped.
CREATE TABLE IF NOT EXISTS rejected_row (
    snapshot_id VARCHAR NOT NULL,
    row_number  INTEGER,
    reason      VARCHAR,
    raw_json    VARCHAR
);

-- ------------------------------------------------------------- computed

CREATE TABLE IF NOT EXISTS threshold_set (
    threshold_set_id VARCHAR PRIMARY KEY,
    entity_id        VARCHAR,
    created_at       TIMESTAMP NOT NULL,
    created_by       VARCHAR,
    payload_json     VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_run (
    run_id           VARCHAR PRIMARY KEY,
    entity_id        VARCHAR NOT NULL,
    location_id      VARCHAR NOT NULL,
    as_of_date       DATE NOT NULL,
    created_at       TIMESTAMP NOT NULL,
    threshold_set_id VARCHAR NOT NULL,
    snapshot_ids     VARCHAR NOT NULL,   -- JSON array, the lineage
    engine_version   VARCHAR NOT NULL,
    item_count       INTEGER
);

CREATE TABLE IF NOT EXISTS metric_result (
    run_id              VARCHAR NOT NULL,
    item_key            VARCHAR NOT NULL,
    demand_class        VARCHAR,
    abc_class           VARCHAR,
    service_target      DOUBLE,
    service_metric      VARCHAR,
    safety_stock        DOUBLE,
    reorder_point       DOUBLE,
    order_up_to         DOUBLE,
    suggested_order_qty DOUBLE,
    order_value         DOUBLE,
    months_cover        DOUBLE,
    status              VARCHAR,
    stock_value         DOUBLE,
    excess_value        DOUBLE,
    days_to_expiry      INTEGER,
    reason              VARCHAR
);

-- --------------------------------------------------------------- audit

-- Append only. Never editable from the UI, included in backups.
CREATE TABLE IF NOT EXISTS audit (
    audit_id    VARCHAR PRIMARY KEY,
    at_utc      TIMESTAMP NOT NULL,   -- 'at' is reserved in DuckDB 2.x
    user_id     VARCHAR,
    action      VARCHAR NOT NULL,
    object_type VARCHAR,
    object_id   VARCHAR,
    detail_json VARCHAR
);

-- --------------------------------------------------------------- views

-- Only the snapshots that have not been replaced by a later upload.
CREATE OR REPLACE VIEW current_snapshot AS
SELECT * FROM snapshot
WHERE superseded_by IS NULL AND status <> 'rejected';
