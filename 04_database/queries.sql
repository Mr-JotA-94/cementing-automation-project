-- =============================================================================
-- queries.sql — Analytical SQL Queries for Cementing Operations
-- =============================================================================
-- PURPOSE:
--   These queries demonstrate what SQL is best at: joining, grouping,
--   and aggregating across tables. Run these in pgAdmin to explore the data
--   before Python takes over for advanced processing.
--
-- Each query has a label, a purpose, and an explanation of the logic.
-- These are also good interview discussion pieces.
-- =============================================================================


-- =============================================================================
-- QUERY 1: Job Overview
-- PURPOSE: Quick summary of all jobs — the first thing an ops manager sees.
-- =============================================================================
SELECT
    j.job_id,
    j.location,
    j.start_time,
    j.duration_minutes,
    j.planned_volume_bbls,
    COUNT(s.id)                                         AS total_readings,
    ROUND(AVG(s.pressure_psi)::NUMERIC, 2)              AS avg_pressure_psi,
    ROUND(MAX(s.pressure_psi)::NUMERIC, 2)              AS max_pressure_psi,
    ROUND(AVG(s.flow_rate_bpm)::NUMERIC, 4)             AS avg_flow_rate_bpm,
    SUM(CASE WHEN s.is_anomaly THEN 1 ELSE 0 END)       AS anomaly_reading_count
FROM jobs j
LEFT JOIN sensor_data s ON j.job_id = s.job_id
GROUP BY j.job_id, j.location, j.start_time, j.duration_minutes, j.planned_volume_bbls
ORDER BY j.start_time;

-- WHY LEFT JOIN here:
--   LEFT JOIN keeps all jobs in the result even if they have no sensor data.
--   INNER JOIN would silently drop jobs with missing data — dangerous for reporting.


-- =============================================================================
-- QUERY 2: KPIs by Stage
-- PURPOSE: Compare sensor behavior across operational stages.
--          Answers: "Is our pressure higher during displacement than pumping?"
-- =============================================================================
SELECT
    stage,
    COUNT(*)                                            AS reading_count,
    ROUND(AVG(pressure_psi)::NUMERIC, 2)                AS avg_pressure_psi,
    ROUND(STDDEV(pressure_psi)::NUMERIC, 2)             AS stddev_pressure,
    ROUND(MIN(pressure_psi)::NUMERIC, 2)                AS min_pressure_psi,
    ROUND(MAX(pressure_psi)::NUMERIC, 2)                AS max_pressure_psi,
    ROUND(AVG(flow_rate_bpm)::NUMERIC, 4)               AS avg_flow_bpm,
    ROUND(AVG(density_ppg)::NUMERIC, 4)                 AS avg_density_ppg
FROM sensor_data
GROUP BY stage
ORDER BY
    CASE stage                                          -- Order stages operationally, not alphabetically
        WHEN 'idle'         THEN 1
        WHEN 'pumping'      THEN 2
        WHEN 'displacement' THEN 3
        WHEN 'flush'        THEN 4
    END;


-- =============================================================================
-- QUERY 3: Non-Productive Time (NPT) per Job
-- PURPOSE: Identify which jobs had the most downtime.
--          NPT = time where stage reverted to 'idle' mid-job (fault pauses).
--
-- LOGIC:
--   Normal idle is only at the START of a job (pre-job setup).
--   If idle appears after pumping has already started → that's NPT.
--   We detect this by checking if idle readings appear after the first
--   pumping reading for each job.
-- =============================================================================
WITH first_pump AS (
    -- Find the timestamp when pumping first started per job
    SELECT
        job_id,
        MIN(timestamp) AS pump_start
    FROM sensor_data
    WHERE stage = 'pumping'
    GROUP BY job_id
),
npt_readings AS (
    -- Count idle readings that occurred AFTER pumping started (those are NPT)
    SELECT
        s.job_id,
        COUNT(*) AS npt_readings
    FROM sensor_data s
    JOIN first_pump fp ON s.job_id = fp.job_id
    WHERE s.stage = 'idle'
      AND s.timestamp > fp.pump_start    -- After pumping started = unplanned idle = NPT
    GROUP BY s.job_id
)
SELECT
    j.job_id,
    j.duration_minutes                                          AS total_duration_min,
    COALESCE(nr.npt_readings, 0)                                AS npt_minutes,
    ROUND(
        (COALESCE(nr.npt_readings, 0)::NUMERIC / j.duration_minutes) * 100, 2
    )                                                           AS npt_pct,
    j.planned_volume_bbls
FROM jobs j
LEFT JOIN npt_readings nr ON j.job_id = nr.job_id
ORDER BY npt_pct DESC NULLS LAST;

-- NOTE: COALESCE(nr.npt_readings, 0) handles jobs with no NPT.
--       Without it, jobs with zero NPT would show NULL instead of 0.


-- =============================================================================
-- QUERY 4: Pressure Stability Index per Job (during pumping only)
-- PURPOSE: Identify unstable jobs. High variance during pumping suggests
--          equipment issues or formation problems.
--
-- FORMULA: Coefficient of Variation = STDDEV / AVG
--          Lower is better. Above 0.15 is worth investigating.
-- =============================================================================
SELECT
    job_id,
    COUNT(*)                                                    AS pumping_readings,
    ROUND(AVG(pressure_psi)::NUMERIC, 2)                        AS avg_pressure,
    ROUND(STDDEV(pressure_psi)::NUMERIC, 2)                     AS stddev_pressure,
    ROUND(
        (STDDEV(pressure_psi) / NULLIF(AVG(pressure_psi), 0))::NUMERIC, 4
    )                                                           AS pressure_stability_idx
FROM sensor_data
WHERE stage = 'pumping'               -- Only analyze pumping — other stages have different norms
  AND is_anomaly = FALSE              -- Exclude known anomalies from baseline calculation
GROUP BY job_id
ORDER BY pressure_stability_idx DESC; -- Most unstable jobs first

-- WHY NULLIF(AVG, 0):
--   Prevents division by zero. If somehow average pressure is 0 (shouldn't happen
--   during pumping, but defensive coding is good practice), returns NULL instead
--   of a crash.


-- =============================================================================
-- QUERY 5: Anomaly Summary
-- PURPOSE: Which jobs had the most anomalies? What types?
--          Good for the alerts page of the Power BI dashboard.
-- =============================================================================
SELECT
    job_id,
    fault_type,
    COUNT(*)                                                    AS occurrence_count,
    MIN(timestamp)                                              AS first_occurrence,
    MAX(timestamp)                                              AS last_occurrence
FROM sensor_data
WHERE is_anomaly = TRUE
  AND fault_type IS NOT NULL
GROUP BY job_id, fault_type
ORDER BY job_id, first_occurrence;


-- =============================================================================
-- QUERY 6: Volume Pumped vs Planned
-- PURPOSE: Calculate actual volume pumped and compare to plan.
--
-- FORMULA:
--   Volume (bbls) = flow_rate (bpm) × time_interval (minutes)
--   Since each reading = 1 minute interval, we SUM flow_rate across
--   pumping + displacement readings to get total volume.
-- =============================================================================
SELECT
    s.job_id,
    j.planned_volume_bbls,
    ROUND(
        SUM(
            CASE
                WHEN s.stage IN ('pumping', 'displacement') THEN s.flow_rate_bpm
                ELSE 0
            END
        )::NUMERIC, 2
    )                                                           AS actual_volume_bbls,
    ROUND(
        (
            SUM(CASE WHEN s.stage IN ('pumping', 'displacement') THEN s.flow_rate_bpm ELSE 0 END)
            / NULLIF(j.planned_volume_bbls, 0)
        ) * 100, 2
    )                                                           AS volume_efficiency_pct
FROM sensor_data s
JOIN jobs j ON s.job_id = j.job_id
GROUP BY s.job_id, j.planned_volume_bbls
ORDER BY volume_efficiency_pct ASC;  -- Worst performing jobs first


-- =============================================================================
-- QUERY 7: Stage Duration Summary
-- PURPOSE: How long did each stage last per job?
--          Uses job_stages (pre-aggregated) — much faster than scanning sensor_data.
-- =============================================================================
SELECT
    job_id,
    MAX(CASE WHEN stage = 'idle'         THEN duration_min END) AS idle_min,
    MAX(CASE WHEN stage = 'pumping'      THEN duration_min END) AS pumping_min,
    MAX(CASE WHEN stage = 'displacement' THEN duration_min END) AS displacement_min,
    MAX(CASE WHEN stage = 'flush'        THEN duration_min END) AS flush_min,
    SUM(duration_min)                                           AS total_min
FROM job_stages
GROUP BY job_id
ORDER BY job_id;

-- WHY MAX(CASE WHEN ...):
--   This is a pivot — turning rows into columns. Each stage becomes its own column.
--   MAX() is used because there's only one row per stage per job (due to UNIQUE
--   constraint), so MAX() of a single value just returns that value.
