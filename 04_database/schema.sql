-- =============================================================================
-- schema.sql — Cementing Pipeline Database Schema
-- Database: PostgreSQL 15+
-- =============================================================================
-- EXECUTION ORDER MATTERS:
--   Run this file once to set up the database from scratch.
--   Tables with foreign keys must be created AFTER the tables they reference.
--   Order: jobs → sensor_data → job_stages → events → job_summary → alerts
--
-- To run this file:
--   psql -U postgres -d cementing_db -f schema.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- SAFETY: Drop tables if they already exist (clean slate on re-run)
-- ORDER: child tables first, then parent tables (reverse of creation order)
-- WHY: You can't drop a table that another table's foreign key points to.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS alerts      CASCADE;
DROP TABLE IF EXISTS job_summary CASCADE;
DROP TABLE IF EXISTS events      CASCADE;
DROP TABLE IF EXISTS job_stages  CASCADE;
DROP TABLE IF EXISTS sensor_data CASCADE;
DROP TABLE IF EXISTS jobs        CASCADE;


-- =============================================================================
-- TABLE 1: jobs
-- PURPOSE: One row per cementing job. The "master" table everything links to.
-- ANALOGY: Think of this as the job work order. All other tables are details.
-- =============================================================================
CREATE TABLE jobs (
    job_id               VARCHAR(20)    PRIMARY KEY,   -- e.g. 'JOB-001'
    start_time           TIMESTAMP      NOT NULL,
    end_time             TIMESTAMP      NOT NULL,
    duration_minutes     INTEGER        NOT NULL,
    planned_volume_bbls  NUMERIC(10,2)  NOT NULL,      -- Barrels planned before job starts
    location             VARCHAR(50),                  -- Well identifier
    has_faults           BOOLEAN        DEFAULT FALSE,

    -- Data quality constraint: job can't end before it starts
    CONSTRAINT chk_job_times CHECK (end_time > start_time)
);

COMMENT ON TABLE  jobs                      IS 'Master table: one row per cementing job';
COMMENT ON COLUMN jobs.planned_volume_bbls  IS 'Target volume agreed before job, in barrels';
COMMENT ON COLUMN jobs.has_faults           IS 'TRUE if simulation injected any fault into this job';


-- =============================================================================
-- TABLE 2: sensor_data
-- PURPOSE: Time-series sensor readings. The largest table — one row per minute
--          per job. This is the raw operational data from the SCADA system.
-- =============================================================================
CREATE TABLE sensor_data (
    id               BIGSERIAL      PRIMARY KEY,        -- Auto-incrementing surrogate key
    timestamp        TIMESTAMP      NOT NULL,
    job_id           VARCHAR(20)    NOT NULL,
    pressure_psi     NUMERIC(10,2),
    flow_rate_bpm    NUMERIC(8,4),
    density_ppg      NUMERIC(8,4),
    stage            VARCHAR(20)    NOT NULL,
    is_anomaly       BOOLEAN        DEFAULT FALSE,      -- Ground truth from simulation
    fault_type       VARCHAR(50),                       -- NULL if no fault

    -- Foreign key: every sensor reading must belong to a real job
    CONSTRAINT fk_sensor_job
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON DELETE CASCADE,                              -- If a job is deleted, its readings go too

    -- Data quality constraints: physical limits
    CONSTRAINT chk_pressure    CHECK (pressure_psi    >= 0),
    CONSTRAINT chk_flow        CHECK (flow_rate_bpm   >= 0),
    CONSTRAINT chk_density     CHECK (density_ppg     >= 7.0),
    CONSTRAINT chk_stage       CHECK (stage IN ('idle', 'pumping', 'displacement', 'flush'))
);

-- INDEX: timestamp queries are extremely common in time-series data.
-- Without this index, every time-range query scans the entire table.
-- With it, PostgreSQL jumps directly to the relevant rows.
CREATE INDEX idx_sensor_timestamp ON sensor_data(timestamp);
CREATE INDEX idx_sensor_job_id    ON sensor_data(job_id);
CREATE INDEX idx_sensor_stage     ON sensor_data(stage);

COMMENT ON TABLE  sensor_data           IS 'Time-series SCADA readings: one row per minute per job';
COMMENT ON COLUMN sensor_data.id        IS 'Surrogate key — sensor readings have no natural unique ID';
COMMENT ON COLUMN sensor_data.is_anomaly IS 'Ground truth label injected during simulation';


-- =============================================================================
-- TABLE 3: job_stages
-- PURPOSE: Explicit start/end timestamps for each stage within a job.
--          Answers: "When exactly did pumping start and end for JOB-007?"
--
-- WHY THIS TABLE EXISTS (important to understand):
--   sensor_data already has a 'stage' column — so why store stage boundaries
--   separately? Because scanning sensor_data to find when a stage started
--   means reading potentially thousands of rows per query. job_stages gives
--   you that answer in a single row lookup. This is called "pre-aggregation"
--   and it's a standard data engineering pattern for time-series systems.
-- =============================================================================
CREATE TABLE job_stages (
    id             SERIAL         PRIMARY KEY,
    job_id         VARCHAR(20)    NOT NULL,
    stage          VARCHAR(20)    NOT NULL,
    stage_start    TIMESTAMP      NOT NULL,
    stage_end      TIMESTAMP      NOT NULL,
    duration_min   INTEGER        NOT NULL,             -- Computed: (stage_end - stage_start) in minutes

    CONSTRAINT fk_stages_job
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_stage_times  CHECK (stage_end > stage_start),
    CONSTRAINT chk_stage_name   CHECK (stage IN ('idle', 'pumping', 'displacement', 'flush')),

    -- A job can only have one entry per stage (no duplicates)
    CONSTRAINT uq_job_stage     UNIQUE (job_id, stage)
);

CREATE INDEX idx_stages_job_id ON job_stages(job_id);

COMMENT ON TABLE job_stages IS 'Pre-aggregated stage boundaries — avoids scanning sensor_data for timing queries';


-- =============================================================================
-- TABLE 4: events
-- PURPOSE: Log of significant operational events during a job.
--          Unlike sensor_data (continuous), events are discrete moments.
--          Examples: equipment shutdown, crew change, cement returns observed.
--
-- DIFFERENCE FROM alerts:
--   events  = operational log (what happened, manually or system-recorded)
--   alerts  = system-generated flags from rule-based detection (Session 5)
-- =============================================================================
CREATE TABLE events (
    id            SERIAL        PRIMARY KEY,
    timestamp     TIMESTAMP     NOT NULL,
    job_id        VARCHAR(20)   NOT NULL,
    event_type    VARCHAR(50)   NOT NULL,               -- e.g. 'shutdown', 'crew_change'
    severity      VARCHAR(20)   DEFAULT 'info',         -- 'info', 'warning', 'critical'
    description   TEXT,

    CONSTRAINT fk_events_job
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_severity
        CHECK (severity IN ('info', 'warning', 'critical'))
);

CREATE INDEX idx_events_job_id   ON events(job_id);
CREATE INDEX idx_events_severity ON events(severity);

COMMENT ON TABLE events IS 'Discrete operational events — distinct from continuous sensor alerts';


-- =============================================================================
-- TABLE 5: job_summary
-- PURPOSE: Pre-computed KPIs for each job. Power BI reads THIS table,
--          not sensor_data directly. This is the "reporting layer."
--
-- WHY PRE-COMPUTE?
--   Power BI refreshes can trigger hundreds of queries. If every refresh
--   recomputed NPT% by scanning 1,488 sensor rows, it would be slow and
--   wasteful. Pre-computing once (in Python) and storing results here means
--   Power BI reads one row per job — instant.
--
-- This table is populated by 05_processing/kpis.py — NOT by this SQL file.
-- =============================================================================
CREATE TABLE job_summary (
    job_id                  VARCHAR(20)   PRIMARY KEY,

    -- Timing KPIs
    total_duration_min      INTEGER,
    pumping_time_min        INTEGER,
    npt_time_min            INTEGER,
    npt_pct                 NUMERIC(6,2),               -- Non-Productive Time %

    -- Pressure KPIs
    avg_pressure_psi        NUMERIC(10,2),
    max_pressure_psi        NUMERIC(10,2),
    pressure_stability_idx  NUMERIC(8,4),               -- Coefficient of variation (std/mean)

    -- Volume KPIs
    actual_volume_bbls      NUMERIC(10,2),
    planned_volume_bbls     NUMERIC(10,2),
    volume_efficiency_pct   NUMERIC(6,2),

    -- Alert counts
    n_pressure_alerts       INTEGER       DEFAULT 0,
    n_flow_alerts           INTEGER       DEFAULT 0,
    n_stuck_sensor_alerts   INTEGER       DEFAULT 0,
    n_total_alerts          INTEGER       DEFAULT 0,

    -- Job outcome
    job_success             BOOLEAN,                    -- Composite rule — defined in kpis.py
    failure_reason          TEXT,                       -- NULL if successful, else explains why

    -- Metadata
    computed_at             TIMESTAMP     DEFAULT NOW(),

    CONSTRAINT fk_summary_job
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE  job_summary                    IS 'Pre-computed KPIs per job — primary Power BI source';
COMMENT ON COLUMN job_summary.pressure_stability_idx IS 'Coefficient of variation: std/mean during pumping. Higher = more unstable.';
COMMENT ON COLUMN job_summary.job_success        IS 'TRUE if volume_efficiency>=90%, npt_pct<=10%, zero critical alerts';


-- =============================================================================
-- TABLE 6: alerts
-- PURPOSE: Log of rule-based alerts generated by 06_anomaly/detect.py.
--          One row per alert event (a single fault can generate multiple alerts
--          if it persists across multiple readings).
-- =============================================================================
CREATE TABLE alerts (
    id              BIGSERIAL     PRIMARY KEY,
    job_id          VARCHAR(20)   NOT NULL,
    timestamp       TIMESTAMP     NOT NULL,
    alert_type      VARCHAR(50)   NOT NULL,             -- 'pressure_spike', 'flow_dropout', 'stuck_sensor'
    severity        VARCHAR(20)   NOT NULL,             -- 'warning', 'critical'
    sensor_value    NUMERIC(10,4),                      -- The actual reading that triggered the alert
    threshold_value NUMERIC(10,4),                      -- The threshold it crossed
    stage           VARCHAR(20),                        -- Which stage was active
    description     TEXT,

    CONSTRAINT fk_alerts_job
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_alert_severity
        CHECK (severity IN ('warning', 'critical')),

    CONSTRAINT chk_alert_type
        CHECK (alert_type IN ('pressure_spike', 'flow_dropout', 'stuck_sensor', 'npt_pause', 'under_pressure'))
);

CREATE INDEX idx_alerts_job_id    ON alerts(job_id);
CREATE INDEX idx_alerts_type      ON alerts(alert_type);
CREATE INDEX idx_alerts_severity  ON alerts(severity);
CREATE INDEX idx_alerts_timestamp ON alerts(timestamp);

COMMENT ON TABLE alerts IS 'Rule-based alert log generated by anomaly detection — one row per alert instance';


-- =============================================================================
-- VERIFICATION QUERIES
-- Run these after loading data to confirm everything looks right.
-- =============================================================================

-- Check table structure was created correctly
SELECT
    table_name,
    COUNT(*) AS column_count
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('jobs','sensor_data','job_stages','events','job_summary','alerts')
GROUP BY table_name
ORDER BY table_name;
