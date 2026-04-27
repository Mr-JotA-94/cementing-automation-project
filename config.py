# =============================================================================
# config.py — Central configuration for the Cementing Pipeline
# =============================================================================
# ALL thresholds, constants, and paths live here.
# Change a value once → it propagates to every script that imports this file.
# Never hardcode these values inside individual scripts.
# =============================================================================

from pathlib import Path

# -----------------------------------------------------------------------------
# PROJECT PATHS
# -----------------------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent
STAGING_DIR  = BASE_DIR / "02_staging" / "raw"
OUTPUT_DIR   = BASE_DIR / "07_output"

# Ensure output directories exist when config is imported
STAGING_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# SIMULATION PARAMETERS
# -----------------------------------------------------------------------------
NUM_JOBS           = 10        # How many cementing jobs to simulate
SAMPLE_INTERVAL_S  = 60        # Seconds between sensor readings (1-minute intervals)
RANDOM_SEED        = 42        # Reproducibility — same seed = same data every run


# Job duration bounds (in minutes)
JOB_DURATION_MIN   = 90        # Shortest possible job
JOB_DURATION_MAX   = 240       # Longest possible job


# -----------------------------------------------------------------------------
# OPERATIONAL STAGE DEFINITIONS
# These are the four phases of a cementing job, in order.
# Each tuple is (stage_name, fraction_of_total_job_time)
# Fractions must sum to 1.0
# -----------------------------------------------------------------------------
STAGE_PROFILE = [
    ("idle",         0.10),   # Equipment setup, pre-checks
    ("pumping",      0.50),   # Main cement pumping phase
    ("displacement", 0.25),   # Pushing cement with displacement fluid
    ("flush",        0.15),   # Final flush of lines
]


# -----------------------------------------------------------------------------
# SENSOR BASELINE VALUES (normal operating ranges per stage)
# Format: stage_name: {"sensor": (mean, std_deviation)}
# These reflect realistic oilfield cementing parameters.
# -----------------------------------------------------------------------------
SENSOR_BASELINES = {
    "idle": {
        "pressure_psi":   (50,   10),    # Near-zero pressure, slight noise
        "flow_rate_bpm":  (0,    0.1),   # No flow
        "density_ppg":    (8.33, 0.05),  # Near water density (fresh water = 8.33 ppg)
    },
    "pumping": {
        "pressure_psi":   (2500, 150),   # Main pumping pressure — high and stable
        "flow_rate_bpm":  (4.5,  0.3),   # Typical cementing flow rate
        "density_ppg":    (15.5, 0.2),   # Cement slurry density (heavier than water)
    },
    "displacement": {
        "pressure_psi":   (3200, 200),   # Peak pressure — pushing cement into place
        "flow_rate_bpm":  (3.5,  0.4),   # Slightly lower flow rate
        "density_ppg":    (8.6,  0.1),   # Displacement fluid (lighter than cement)
    },
    "flush": {
        "pressure_psi":   (800,  100),   # Pressure drops as lines are cleared
        "flow_rate_bpm":  (2.0,  0.3),   # Low flow rate
        "density_ppg":    (8.33, 0.05),  # Water-like flush fluid
    },
}


# -----------------------------------------------------------------------------
# ANOMALY INJECTION PARAMETERS
# Controls how realistic faults are introduced into the simulation.
# Each job has a probability of receiving each fault type.
# -----------------------------------------------------------------------------
ANOMALY_CONFIG = {
    "pressure_spike": {
        "probability":      0.4,          # 40% of jobs get at least one spike
        "magnitude_factor": 1.4,          # Spike = 40% above current pressure
        "duration_readings": (3, 8),      # Spike lasts 3–8 readings
    },
    "flow_dropout": {
        "probability":      0.3,          # 30% of jobs get a flow dropout
        "min_flow":         0.1,          # Flow drops to near-zero (not exactly 0 — sensors jitter)
        "duration_readings": (2, 10),     # Lasts 2–10 readings (2–10 minutes)
    },
    "stuck_sensor": {
        "probability":      0.25,         # 25% of jobs get a stuck sensor
        "affected_sensor":  "pressure_psi",
        "duration_readings": (10, 20),    # Stuck for 10–20 readings
    },
    "npt_pause": {
        "probability":      0.35,         # 35% of jobs have a non-productive pause
        "duration_readings": (5, 25),     # Pause lasts 5–25 minutes
    },
}


# -----------------------------------------------------------------------------
# ALERT / ANOMALY DETECTION THRESHOLDS
# Used in 06_anomaly/detect.py — rule-based detection layer.
# These are the limits an operations engineer would configure.
# -----------------------------------------------------------------------------
ALERT_THRESHOLDS = {
    # Hard limits — absolute values, stage-independent
    "pressure_max_psi":       4500,    # Above this = critical pressure alert
    "pressure_min_pumping":   1500,    # Below this during pumping = under-pressure warning

    # Flow rate limits (only checked during pumping and displacement stages)
    "flow_min_bpm":           0.5,     # Below this during active pumping = flow dropout alert
    "flow_max_bpm":           8.0,     # Above this = potential equipment issue

    # Density limits
    "density_max_ppg":        18.0,    # Max allowable slurry density
    "density_min_ppg":        8.0,     # Below this during pumping = slurry loss warning

    # Statistical anomaly detection (rolling window z-score)
    "rolling_window_readings": 10,     # Window size for rolling mean/std (10 minutes)
    "z_score_threshold":       3.0,    # Flag if value > mean + (3 × std) within the window

    # Stuck sensor detection
    "stuck_sensor_readings":   8,      # Flag if same value repeats for 8+ consecutive readings
    "stuck_sensor_tolerance":  0.01,   # Tolerance: values within ±0.01 count as "same"
}


# -----------------------------------------------------------------------------
# KPI THRESHOLDS — used to classify job success/failure
# These mirror what an operations manager would use to evaluate a job.
# -----------------------------------------------------------------------------
KPI_THRESHOLDS = {
    "volume_efficiency_min_pct": 90.0,  # Job needs ≥ 90% of planned volume pumped
    "npt_max_pct":               10.0,  # Non-productive time must be ≤ 10% of job duration
    "max_critical_alerts":        0,    # Zero critical pressure alerts for a successful job
}


# -----------------------------------------------------------------------------
# DATABASE — loaded from .env at runtime (see load_data.py)
# Only non-secret defaults here.
# -----------------------------------------------------------------------------
DB_SCHEMA = "public"          # PostgreSQL schema (default)
DB_TABLE_SENSOR  = "sensor_data"
DB_TABLE_JOBS    = "jobs"
DB_TABLE_STAGES  = "job_stages"
DB_TABLE_EVENTS  = "events"
DB_TABLE_SUMMARY = "job_summary"
DB_TABLE_ALERTS  = "alerts"


# -----------------------------------------------------------------------------
# POWER BI OUTPUT FILENAMES
# These are the CSVs that 07_output/export.py will produce.
# -----------------------------------------------------------------------------
EXPORT_FILES = {
    "job_summary":   OUTPUT_DIR / "pbi_job_summary.csv",
    "time_series":   OUTPUT_DIR / "pbi_time_series.csv",
    "alerts":        OUTPUT_DIR / "pbi_alerts.csv",
    "stage_summary": OUTPUT_DIR / "pbi_stage_summary.csv",
}
