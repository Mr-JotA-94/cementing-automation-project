# =============================================================================
# 03_etl/clean.py
# =============================================================================
# PURPOSE:
#   Transform raw staging data into clean, analysis-ready data.
#   Reads from 02_staging/raw/, writes clean version back to 02_staging/.
#
# WHAT THIS SCRIPT DOES:
#   1. Runs validation first (always)
#   2. Cleans sensor data (nulls, types, ranges)
#   3. Engineers basic features (rolling averages, pressure variance flag)
#   4. Tags stuck sensor readings for downstream use
#   5. Saves clean datasets as sensor_data_clean.csv and jobs_clean.csv
#
# IMPORTANT RULE:
#   This script NEVER modifies files in 02_staging/raw/
#   Raw data is sacred — it's the source of truth.
#   Clean data lives at 02_staging/ level (one folder up from raw/).
# =============================================================================

import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, ALERT_THRESHOLDS
from validate import run_validation


# Output path: one level above raw/ — still in staging, but clean
CLEAN_DIR = STAGING_DIR.parent


# =============================================================================
# STEP 1: Clean jobs table
# =============================================================================
def clean_jobs(jobs_df):
    """
    Jobs table is already fairly clean from simulation.
    We standardize types and ensure consistency.
    """
    df = jobs_df.copy()   # Never modify the original DataFrame in place

    # Ensure datetime types (may come in as strings from CSV)
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["end_time"]   = pd.to_datetime(df["end_time"])

    # Standardize job_id format (uppercase, trimmed)
    df["job_id"] = df["job_id"].str.strip().str.upper()

    # Ensure boolean type (CSV sometimes reads as string 'True'/'False')
    df["has_faults"] = df["has_faults"].astype(bool)

    # Round planned volume to 2 decimal places
    df["planned_volume_bbls"] = df["planned_volume_bbls"].round(2)

    return df


# =============================================================================
# STEP 2: Clean sensor data
# =============================================================================
def clean_sensor(sensor_df):
    """
    Applies all cleaning rules to sensor_data.
    Each rule is a separate function for clarity and testability.
    """
    df = sensor_df.copy()

    df = _fix_types(df)
    df = _handle_nulls(df)
    df = _clip_physical_limits(df)
    df = _tag_stuck_sensors(df)
    df = _add_rolling_features(df)
    df = _add_derived_columns(df)

    return df


def _fix_types(df):
    """Ensure all columns have the correct data types."""
    df["timestamp"]     = pd.to_datetime(df["timestamp"])
    df["job_id"]        = df["job_id"].str.strip().str.upper()
    df["stage"]         = df["stage"].str.strip().str.lower()
    df["is_anomaly"]    = df["is_anomaly"].astype(bool)

    # fault_type: replace NaN with empty string for cleaner downstream handling
    # We use empty string (not None) so string operations don't break
    df["fault_type"]    = df["fault_type"].fillna("none")

    # Round sensor values to realistic precision
    df["pressure_psi"]  = df["pressure_psi"].round(2)
    df["flow_rate_bpm"] = df["flow_rate_bpm"].round(4)
    df["density_ppg"]   = df["density_ppg"].round(4)

    return df


def _handle_nulls(df):
    """
    Handle missing sensor values.
    Strategy: forward-fill within each job (last known good value).

    WHY FORWARD-FILL (not mean/median)?
        In time-series data, the most recent reading is the best estimate
        of the current state. A mean would introduce values that never
        actually existed in the sensor timeline — that's misleading for
        operational analysis.

    WHY PER JOB (not globally)?
        Forward-filling across job boundaries would carry the last reading
        of JOB-001 into the first reading of JOB-002. That's nonsensical.
    """
    sensor_cols = ["pressure_psi", "flow_rate_bpm", "density_ppg"]
    null_counts = df[sensor_cols].isnull().sum()

    if null_counts.sum() > 0:
        print(f"  Filling nulls (forward-fill per job): {null_counts.to_dict()}")
        df = df.sort_values(["job_id", "timestamp"])
        df[sensor_cols] = (
            df.groupby("job_id")[sensor_cols]
            .transform(lambda x: x.ffill().bfill())
            # ffill: fill with previous value
            # bfill: if first reading is null, fill with next value
        )

    return df


def _clip_physical_limits(df):
    """
    Clip values to physically possible ranges.
    These are hard limits — any reading outside them is a sensor error.

    CLIP vs DROP:
        We clip (bring to boundary) rather than drop (remove the row).
        Dropping rows breaks the time-series continuity.
        Clipping preserves the row but corrects the impossible value.
    """
    original_len = len(df)

    # Pressure: cannot be negative; cap at 1.5x the max alert threshold
    max_pressure = ALERT_THRESHOLDS["pressure_max_psi"] * 1.5
    df["pressure_psi"] = df["pressure_psi"].clip(lower=0, upper=max_pressure)

    # Flow rate: cannot be negative; cap at 1.5x max flow threshold
    max_flow = ALERT_THRESHOLDS["flow_max_bpm"] * 1.5
    df["flow_rate_bpm"] = df["flow_rate_bpm"].clip(lower=0, upper=max_flow)

    # Density: minimum is fresh water (8.33 ppg); max realistic cement = 20 ppg
    df["density_ppg"] = df["density_ppg"].clip(lower=7.0, upper=20.0)

    return df


def _tag_stuck_sensors(df):
    """
    Add a boolean column 'is_stuck_sensor' flagging readings where
    a sensor value hasn't changed for N+ consecutive readings.

    This is separate from is_anomaly (which comes from simulation ground truth).
    In a real pipeline, you wouldn't have ground truth — you'd rely on this.

    WHY THIS MATTERS FOR ANALYSIS:
        Stuck sensor readings look like perfectly stable operations.
        If included in KPI calculations, they artificially reduce variance
        and make jobs look more stable than they were.
        We tag them so downstream scripts can exclude them from KPI math.
    """
    stuck_threshold = ALERT_THRESHOLDS["stuck_sensor_readings"]
    tolerance       = ALERT_THRESHOLDS["stuck_sensor_tolerance"]

    df = df.sort_values(["job_id", "timestamp"]).reset_index(drop=True)
    df["is_stuck_sensor"] = False

    for job_id, group in df.groupby("job_id"):
        idx = group.index

        for col in ["pressure_psi", "flow_rate_bpm", "density_ppg"]:
            values    = group[col].values
            n         = len(values)
            run_count = 1

            for i in range(1, n):
                if abs(values[i] - values[i-1]) < tolerance:
                    run_count += 1
                    if run_count >= stuck_threshold:
                        # Tag from the start of this run backward
                        start = i - run_count + 1
                        df.loc[idx[start:i+1], "is_stuck_sensor"] = True
                else:
                    run_count = 1

    stuck_count = df["is_stuck_sensor"].sum()
    if stuck_count > 0:
        print(f"  Stuck sensor readings tagged: {stuck_count}")

    return df


def _add_rolling_features(df):
    """
    Add rolling window features — computed per job, per stage.

    Features added:
        pressure_rolling_mean : smoothed pressure trend (reduces noise)
        pressure_rolling_std  : rolling standard deviation (instability indicator)
        flow_rolling_mean     : smoothed flow trend

    WHY PER JOB (not globally)?
        Rolling across jobs would mix readings from different wells and
        time periods. The window must stay within a single job's context.

    WINDOW SIZE: 10 readings = 10 minutes. Chosen because:
        - Short enough to detect rapid changes (spikes)
        - Long enough to smooth out normal sensor noise
    """
    window = ALERT_THRESHOLDS["rolling_window_readings"]

    df = df.sort_values(["job_id", "timestamp"]).reset_index(drop=True)

    results = []
    for job_id, group in df.groupby("job_id"):
        group = group.copy()
        group["pressure_rolling_mean"] = (
            group["pressure_psi"]
            .rolling(window=window, min_periods=3)
            .mean().round(2)
        )
        group["pressure_rolling_std"] = (
            group["pressure_psi"]
            .rolling(window=window, min_periods=3)
            .std().round(2)
        )
        group["flow_rolling_mean"] = (
            group["flow_rate_bpm"]
            .rolling(window=window, min_periods=3)
            .mean().round(4)
        )
        results.append(group)
    df = pd.concat(results, ignore_index=True)

    return df


def _add_derived_columns(df):
    """
    Add columns derived from existing data.
    These don't require rolling windows — just row-level calculations.
    """
    # Pressure deviation from rolling mean (how far is this reading from recent trend?)
    # Positive = above trend, Negative = below trend
    df["pressure_deviation"] = (
        (df["pressure_psi"] - df["pressure_rolling_mean"])
        .round(2)
    )

    # Is this reading during an "active" stage? (used frequently in filtering)
    df["is_active_stage"] = df["stage"].isin(["pumping", "displacement"])

    return df


# =============================================================================
# MAIN
# =============================================================================
def run_cleaning(save=True, verbose=True):
    """
    Full ETL pipeline: validate → clean → save.

    Returns:
        (jobs_clean_df, sensor_clean_df)
    """
    if verbose:
        print("=" * 55)
        print("ETL CLEANING PIPELINE")
        print("=" * 55)

    # Load raw data
    jobs_df   = pd.read_csv(STAGING_DIR / "jobs_raw.csv",
                            parse_dates=["start_time", "end_time"])
    sensor_df = pd.read_csv(STAGING_DIR / "sensor_data_raw.csv",
                            parse_dates=["timestamp"])

    # Step 1: Validate FIRST — always
    if verbose:
        print("\n[1/3] Validating raw data...")
    result = run_validation(jobs_df, sensor_df, verbose=verbose)

    # Stop if critical issues found — don't clean broken data
    critical_issues = [i for i in result["sensor_issues"] if "NULL" in i or "negative" in i.lower()]
    if critical_issues:
        print("\n  CRITICAL issues found — cleaning aborted.")
        print("  Fix source data before proceeding.")
        return None, None

    # Step 2: Clean
    if verbose:
        print("\n[2/3] Cleaning data...")
    jobs_clean   = clean_jobs(jobs_df)
    sensor_clean = clean_sensor(sensor_df)

    # Step 3: Save
    if save:
        if verbose:
            print("\n[3/3] Saving clean data...")

        jobs_path   = CLEAN_DIR / "jobs_clean.csv"
        sensor_path = CLEAN_DIR / "sensor_data_clean.csv"

        jobs_clean.to_csv(jobs_path,     index=False)
        sensor_clean.to_csv(sensor_path, index=False)

        if verbose:
            print(f"  ✓ Saved: {jobs_path.name}")
            print(f"  ✓ Saved: {sensor_path.name} ({len(sensor_clean):,} rows, "
                  f"{len(sensor_clean.columns)} columns)")

    # Summary
    if verbose:
        new_cols = [c for c in sensor_clean.columns
                    if c not in pd.read_csv(STAGING_DIR / "sensor_data_raw.csv", nrows=0).columns]
        print(f"\n  New columns added: {new_cols}")
        print(f"\n{'=' * 55}")
        print("  ETL COMPLETE")
        print(f"{'=' * 55}")

    return jobs_clean, sensor_clean


if __name__ == "__main__":
    jobs_clean, sensor_clean = run_cleaning(save=True, verbose=True)

    if sensor_clean is not None:
        print("\nClean data preview (new columns):")
        preview_cols = ["job_id", "timestamp", "stage", "pressure_psi",
                        "pressure_rolling_mean", "pressure_rolling_std",
                        "pressure_deviation", "is_stuck_sensor", "is_active_stage"]
        print(sensor_clean[preview_cols].head(15).to_string(index=False))
