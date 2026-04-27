# =============================================================================
# 01_simulation/simulate_jobs.py
# =============================================================================
# PURPOSE:
#   Generate realistic synthetic SCADA time-series data for multiple
#   cementing jobs. Output is saved to 02_staging/raw/ as CSV files.
#
# WHY THIS APPROACH:
#   Real SCADA data can't be shared (confidential). Simulation lets us:
#   - Control exactly what anomalies exist (so we can validate detection)
#   - Generate reproducible datasets (RANDOM_SEED in config.py)
#   - Demonstrate realistic operational patterns to interviewers
#
# OUTPUT FILES:
#   - 02_staging/raw/jobs_raw.csv         → one row per job (metadata)
#   - 02_staging/raw/sensor_data_raw.csv  → time-series readings
#   - 02_staging/raw/ground_truth.csv     → injected faults log (for validation)
# =============================================================================

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import sys
from pathlib import Path

# Allow imports from the project root (where config.py lives)
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (
    NUM_JOBS, SAMPLE_INTERVAL_S, RANDOM_SEED,
    JOB_DURATION_MIN, JOB_DURATION_MAX,
    STAGE_PROFILE, SENSOR_BASELINES, ANOMALY_CONFIG,
    STAGING_DIR
)

# Set random seed for reproducibility
rng = np.random.default_rng(RANDOM_SEED)


# =============================================================================
# HELPER: Generate one stage of sensor readings
# =============================================================================
def generate_stage_readings(stage_name, n_readings, baselines):
    """
    Generate normally distributed sensor readings for a single stage.

    Parameters:
        stage_name  : str   — one of idle, pumping, displacement, flush
        n_readings  : int   — how many time steps to generate
        baselines   : dict  — from config.SENSOR_BASELINES

    Returns:
        DataFrame with columns: pressure_psi, flow_rate_bpm, density_ppg, stage
    
    WHY NORMAL DISTRIBUTION:
        Real sensors produce readings clustered around a mean with natural
        variation. Normal distribution captures this well for stable operations.
        Anomalies are injected separately — they are NOT part of normal variation.
    """
    b = baselines[stage_name]

    pressure  = rng.normal(b["pressure_psi"][0],  b["pressure_psi"][1],  n_readings)
    flow_rate = rng.normal(b["flow_rate_bpm"][0], b["flow_rate_bpm"][1], n_readings)
    density   = rng.normal(b["density_ppg"][0],   b["density_ppg"][1],   n_readings)

    # Physical constraint: values can't be negative
    pressure  = np.clip(pressure,  0, None)
    flow_rate = np.clip(flow_rate, 0, None)
    density   = np.clip(density,   7.0, None)   # Water is 8.33 ppg; allow slight undershoot

    return pd.DataFrame({
        "pressure_psi":  pressure,
        "flow_rate_bpm": flow_rate,
        "density_ppg":   density,
        "stage":         stage_name,
        "is_anomaly":    False,   # Will be updated by fault injection
        "fault_type":    None,
    })


# =============================================================================
# HELPER: Inject faults into a job's sensor DataFrame
# =============================================================================
def inject_faults(df, job_id, anomaly_config):
    """
    Inject realistic SCADA faults into the sensor data.
    Only injects into the pumping/displacement stages (where faults are meaningful).

    Returns:
        (df_modified, fault_log list)
    
    WHY SEPARATE FAULT INJECTION:
        Keeps normal data generation clean. We know exactly which rows are faults,
        so 06_anomaly/detect.py can be validated against ground truth.
    """
    fault_log = []

    # Identify rows that are in active stages (not idle or flush)
    active_mask = df["stage"].isin(["pumping", "displacement"])
    active_idx  = df.index[active_mask].tolist()

    if len(active_idx) < 20:
        return df, fault_log   # Job too short to inject faults safely

    # ------------------------------------------------------------------
    # FAULT 1: Pressure Spike
    # Sudden sharp rise in pressure over a few readings, then recovery.
    # Cause: Annular restriction, tool malfunction, or surge pressure.
    # ------------------------------------------------------------------
    cfg = anomaly_config["pressure_spike"]
    if rng.random() < cfg["probability"]:
        duration  = int(rng.integers(*cfg["duration_readings"]))
        start_pos = rng.integers(5, len(active_idx) - duration - 5)
        spike_idx = active_idx[start_pos: start_pos + duration]

        for i in spike_idx:
            df.at[i, "pressure_psi"] *= cfg["magnitude_factor"]
            df.at[i, "is_anomaly"]   = True
            df.at[i, "fault_type"]   = "pressure_spike"

        fault_log.append({
            "job_id":      job_id,
            "fault_type":  "pressure_spike",
            "start_index": spike_idx[0],
            "end_index":   spike_idx[-1],
            "n_readings":  duration,
        })

    # ------------------------------------------------------------------
    # FAULT 2: Flow Dropout
    # Flow rate drops to near-zero during pumping.
    # Cause: Pump stall, line blockage, or accidental valve closure.
    # ------------------------------------------------------------------
    cfg = anomaly_config["flow_dropout"]
    if rng.random() < cfg["probability"]:
        duration  = int(rng.integers(*cfg["duration_readings"]))
        start_pos = rng.integers(5, len(active_idx) - duration - 5)
        drop_idx  = active_idx[start_pos: start_pos + duration]

        for i in drop_idx:
            df.at[i, "flow_rate_bpm"] = cfg["min_flow"] + rng.random() * 0.05
            df.at[i, "is_anomaly"]    = True
            df.at[i, "fault_type"]    = "flow_dropout"

        fault_log.append({
            "job_id":      job_id,
            "fault_type":  "flow_dropout",
            "start_index": drop_idx[0],
            "end_index":   drop_idx[-1],
            "n_readings":  duration,
        })

    # ------------------------------------------------------------------
    # FAULT 3: Stuck Sensor
    # Sensor repeats the same value for many consecutive readings.
    # Cause: Sensor hardware failure or communication loss.
    # This is a data quality issue — not an operational fault — but it
    # must be detected before analysis, or KPIs will be wrong.
    # ------------------------------------------------------------------
    cfg = anomaly_config["stuck_sensor"]
    if rng.random() < cfg["probability"]:
        duration    = int(rng.integers(*cfg["duration_readings"]))
        sensor_col  = cfg["affected_sensor"]
        start_pos   = rng.integers(5, len(active_idx) - duration - 5)
        stuck_idx   = active_idx[start_pos: start_pos + duration]

        stuck_value = df.at[stuck_idx[0], sensor_col]
        for i in stuck_idx:
            df.at[i, sensor_col]    = stuck_value
            df.at[i, "is_anomaly"]  = True
            df.at[i, "fault_type"]  = "stuck_sensor"

        fault_log.append({
            "job_id":      job_id,
            "fault_type":  "stuck_sensor",
            "start_index": stuck_idx[0],
            "end_index":   stuck_idx[-1],
            "n_readings":  duration,
        })

    # ------------------------------------------------------------------
    # FAULT 4: NPT Pause (Non-Productive Time)
    # All sensor values flatten — job stops mid-operation.
    # Cause: Equipment breakdown, weather, waiting on materials.
    # Unlike other faults, this affects ALL sensors simultaneously.
    # ------------------------------------------------------------------
    cfg = anomaly_config["npt_pause"]
    if rng.random() < cfg["probability"]:
        duration  = int(rng.integers(*cfg["duration_readings"]))
        start_pos = rng.integers(5, len(active_idx) - duration - 5)
        pause_idx = active_idx[start_pos: start_pos + duration]

        for i in pause_idx:
            df.at[i, "pressure_psi"]  = 50  + rng.random() * 5
            df.at[i, "flow_rate_bpm"] = 0   + rng.random() * 0.05
            df.at[i, "density_ppg"]   = 8.33 + rng.random() * 0.02
            df.at[i, "stage"]         = "idle"   # Stage reverts to idle during pause
            df.at[i, "is_anomaly"]    = True
            df.at[i, "fault_type"]    = "npt_pause"

        fault_log.append({
            "job_id":      job_id,
            "fault_type":  "npt_pause",
            "start_index": pause_idx[0],
            "end_index":   pause_idx[-1],
            "n_readings":  duration,
        })

    return df, fault_log


# =============================================================================
# MAIN: Simulate all jobs
# =============================================================================
def simulate_all_jobs():
    """
    Orchestrates simulation of all cementing jobs.
    Returns three DataFrames: jobs_df, sensor_df, ground_truth_df
    """
    all_jobs        = []
    all_sensor_data = []
    all_faults      = []

    # Start date for the simulation (jobs are consecutive with short gaps)
    current_time = datetime(2024, 1, 1, 6, 0, 0)   # Operations start Jan 1 at 6 AM

    for job_num in range(1, NUM_JOBS + 1):
        job_id = f"JOB-{job_num:03d}"

        # ------------------------------------------------------------------
        # STEP 1: Decide how long this job runs
        # ------------------------------------------------------------------
        job_duration_min = int(rng.integers(JOB_DURATION_MIN, JOB_DURATION_MAX + 1))
        total_readings   = job_duration_min   # 1 reading per minute

        start_time = current_time
        end_time   = start_time + timedelta(minutes=job_duration_min)

        # ------------------------------------------------------------------
        # STEP 2: Divide readings across stages
        # STAGE_PROFILE defines what fraction of time each stage gets.
        # ------------------------------------------------------------------
        stage_readings = []
        remaining      = total_readings

        for i, (stage_name, fraction) in enumerate(STAGE_PROFILE):
            if i == len(STAGE_PROFILE) - 1:
                n = remaining   # Last stage gets whatever is left (avoids rounding gaps)
            else:
                n = max(1, round(total_readings * fraction))
                remaining -= n
            stage_readings.append((stage_name, n))

        # ------------------------------------------------------------------
        # STEP 3: Generate sensor readings stage by stage
        # ------------------------------------------------------------------
        job_dfs = []
        for stage_name, n_readings in stage_readings:
            stage_df = generate_stage_readings(stage_name, n_readings, SENSOR_BASELINES)
            job_dfs.append(stage_df)

        job_df = pd.concat(job_dfs, ignore_index=True)

        # ------------------------------------------------------------------
        # STEP 4: Inject faults
        # ------------------------------------------------------------------
        job_df, fault_log = inject_faults(job_df, job_id, ANOMALY_CONFIG)
        all_faults.extend(fault_log)

        # ------------------------------------------------------------------
        # STEP 5: Add timestamp and job_id columns
        # ------------------------------------------------------------------
        timestamps = [
            start_time + timedelta(seconds=i * SAMPLE_INTERVAL_S)
            for i in range(len(job_df))
        ]
        job_df.insert(0, "timestamp", timestamps)
        job_df.insert(1, "job_id",    job_id)

        # ------------------------------------------------------------------
        # STEP 6: Calculate planned volume
        # Planned volume = average expected flow rate × pumping time
        # In barrels: flow_rate (bpm) × duration (minutes)
        # ------------------------------------------------------------------
        pumping_readings    = sum(n for s, n in stage_readings if s in ("pumping", "displacement"))
        planned_volume_bbls = round(
            SENSOR_BASELINES["pumping"]["flow_rate_bpm"][0] * pumping_readings, 1
        )

        # ------------------------------------------------------------------
        # STEP 7: Store job metadata
        # ------------------------------------------------------------------
        all_jobs.append({
            "job_id":              job_id,
            "start_time":          start_time,
            "end_time":            end_time,
            "duration_minutes":    job_duration_min,
            "planned_volume_bbls": planned_volume_bbls,
            "location":            f"Well-{rng.integers(1, 50):02d}",
            "has_faults":          len(fault_log) > 0,
        })

        all_sensor_data.append(job_df)

        # Gap between jobs: 2–8 hours of rig movement / setup
        gap_hours    = rng.integers(2, 9)
        current_time = end_time + timedelta(hours=int(gap_hours))

        print(f"  Simulated {job_id}: {job_duration_min} min, "
              f"{len(job_df)} readings, {len(fault_log)} fault(s)")

    # ------------------------------------------------------------------
    # STEP 8: Combine and return
    # ------------------------------------------------------------------
    jobs_df        = pd.DataFrame(all_jobs)
    sensor_df      = pd.concat(all_sensor_data, ignore_index=True)
    ground_truth_df = pd.DataFrame(all_faults) if all_faults else pd.DataFrame(
        columns=["job_id", "fault_type", "start_index", "end_index", "n_readings"]
    )

    return jobs_df, sensor_df, ground_truth_df


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("CEMENTING PIPELINE — Data Simulation")
    print("=" * 60)
    print(f"Simulating {NUM_JOBS} jobs with 1-minute sensor intervals...\n")

    jobs_df, sensor_df, ground_truth_df = simulate_all_jobs()

    # Save to staging area (raw — untouched, never overwritten by ETL)
    jobs_path      = STAGING_DIR / "jobs_raw.csv"
    sensor_path    = STAGING_DIR / "sensor_data_raw.csv"
    gt_path        = STAGING_DIR / "ground_truth.csv"

    jobs_df.to_csv(jobs_path,      index=False)
    sensor_df.to_csv(sensor_path,  index=False)
    ground_truth_df.to_csv(gt_path, index=False)

    print(f"\n{'=' * 60}")
    print("SIMULATION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Jobs simulated:      {len(jobs_df)}")
    print(f"  Total sensor rows:   {len(sensor_df):,}")
    print(f"  Total faults logged: {len(ground_truth_df)}")
    print(f"\n  Files saved to: {STAGING_DIR}")
    print(f"    → {jobs_path.name}")
    print(f"    → {sensor_path.name}")
    print(f"    → {gt_path.name}")

    # Quick sanity check
    print(f"\n  Sensor data preview:")
    print(sensor_df[["job_id", "timestamp", "stage", "pressure_psi",
                      "flow_rate_bpm", "density_ppg"]].head(8).to_string(index=False))
