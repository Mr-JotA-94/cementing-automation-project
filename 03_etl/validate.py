# =============================================================================
# 03_etl/validate.py
# =============================================================================
# PURPOSE:
#   Check raw data for problems BEFORE cleaning.
#   Validation answers: "What is wrong with this data?"
#   Cleaning answers:   "How do we fix it?"
#
#   Always validate first. If you clean without knowing what's broken,
#   you might silently fix the wrong thing — or miss something entirely.
#
# OUTPUT:
#   Prints a validation report. Returns a dict of issues found.
#   Called by clean.py before any transformation runs.
# =============================================================================

import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, ALERT_THRESHOLDS, SENSOR_BASELINES


def validate_jobs(jobs_df):
    """Check the jobs table for structural and logical issues."""
    issues = []

    # Check for duplicate job IDs
    dupes = jobs_df[jobs_df.duplicated("job_id")]
    if not dupes.empty:
        issues.append(f"DUPLICATE job_ids found: {dupes['job_id'].tolist()}")

    # Check end_time is always after start_time
    bad_times = jobs_df[jobs_df["end_time"] <= jobs_df["start_time"]]
    if not bad_times.empty:
        issues.append(f"Jobs where end_time <= start_time: {bad_times['job_id'].tolist()}")

    # Check planned volume is positive
    bad_vol = jobs_df[jobs_df["planned_volume_bbls"] <= 0]
    if not bad_vol.empty:
        issues.append(f"Jobs with zero/negative planned volume: {bad_vol['job_id'].tolist()}")

    return issues


def validate_sensor(sensor_df):
    """
    Check sensor_data for the four most common real SCADA data problems:
    1. Missing values
    2. Physical impossibilities (negative pressure, density below water)
    3. Stuck sensors (same value repeating)
    4. Timestamp gaps (missing minutes)
    """
    issues = []
    stats  = {}

    # ------------------------------------------------------------------
    # CHECK 1: Missing values
    # fault_type is expected to have nulls (most readings have no fault)
    # Everything else should be complete
    # ------------------------------------------------------------------
    critical_cols = ["timestamp", "job_id", "pressure_psi", "flow_rate_bpm",
                     "density_ppg", "stage"]
    for col in critical_cols:
        n_null = sensor_df[col].isnull().sum()
        if n_null > 0:
            issues.append(f"NULL values in {col}: {n_null} rows")

    stats["total_rows"]   = len(sensor_df)
    stats["null_fault_type"] = sensor_df["fault_type"].isnull().sum()

    # ------------------------------------------------------------------
    # CHECK 2: Physical impossibilities
    # These values cannot exist in reality — flag immediately
    # ------------------------------------------------------------------
    negative_pressure = (sensor_df["pressure_psi"] < 0).sum()
    if negative_pressure > 0:
        issues.append(f"Negative pressure readings: {negative_pressure} rows")

    negative_flow = (sensor_df["flow_rate_bpm"] < 0).sum()
    if negative_flow > 0:
        issues.append(f"Negative flow rate readings: {negative_flow} rows")

    # Density below 7.0 ppg is physically impossible for any oilfield fluid
    low_density = (sensor_df["density_ppg"] < 7.0).sum()
    if low_density > 0:
        issues.append(f"Density below 7.0 ppg (physically impossible): {low_density} rows")

    # Extreme high pressure — above max threshold is a data quality flag
    max_threshold = ALERT_THRESHOLDS["pressure_max_psi"]
    extreme_pressure = (sensor_df["pressure_psi"] > max_threshold * 1.5).sum()
    if extreme_pressure > 0:
        issues.append(f"Pressure exceeding 1.5x max threshold ({max_threshold * 1.5} psi): {extreme_pressure} rows")

    # ------------------------------------------------------------------
    # CHECK 3: Stuck sensor detection
    # A sensor reporting the same value for N+ consecutive readings is broken.
    # We check this per job to avoid false positives across job boundaries.
    # ------------------------------------------------------------------
    stuck_threshold = ALERT_THRESHOLDS["stuck_sensor_readings"]
    tolerance       = ALERT_THRESHOLDS["stuck_sensor_tolerance"]
    stuck_jobs      = []

    for job_id, job_df in sensor_df.groupby("job_id"):
        job_df = job_df.sort_values("timestamp").reset_index(drop=True)

        for col in ["pressure_psi", "flow_rate_bpm", "density_ppg"]:
            # Calculate difference between consecutive readings
            diff = job_df[col].diff().abs()
            # Count consecutive readings where diff < tolerance (effectively same value)
            is_stuck      = diff < tolerance
            # Use cumsum trick to count consecutive runs
            run_id        = (~is_stuck).cumsum()
            run_lengths   = is_stuck.groupby(run_id).sum()
            max_run       = run_lengths.max()

            if max_run >= stuck_threshold:
                stuck_jobs.append(f"{job_id}.{col} (max run: {int(max_run)} readings)")

    if stuck_jobs:
        issues.append(f"Potential stuck sensors detected: {stuck_jobs}")
        stats["stuck_sensor_instances"] = len(stuck_jobs)
    else:
        stats["stuck_sensor_instances"] = 0

    # ------------------------------------------------------------------
    # CHECK 4: Timestamp gaps per job
    # Each job should have consecutive 1-minute readings.
    # Gaps indicate dropped data from the SCADA system.
    # ------------------------------------------------------------------
    expected_interval_s = 60   # 1 minute
    gap_jobs = []

    for job_id, job_df in sensor_df.groupby("job_id"):
        job_df      = job_df.sort_values("timestamp")
        time_diffs  = job_df["timestamp"].diff().dt.total_seconds().dropna()
        gaps        = time_diffs[time_diffs > expected_interval_s * 2]  # More than 2 minutes = gap

        if not gaps.empty:
            gap_jobs.append(f"{job_id} ({len(gaps)} gap(s))")

    if gap_jobs:
        issues.append(f"Timestamp gaps detected in jobs: {gap_jobs}")
        stats["jobs_with_gaps"] = len(gap_jobs)
    else:
        stats["jobs_with_gaps"] = 0

    # ------------------------------------------------------------------
    # CHECK 5: Invalid stage values
    # ------------------------------------------------------------------
    valid_stages  = {"idle", "pumping", "displacement", "flush"}
    invalid_stages = sensor_df[~sensor_df["stage"].isin(valid_stages)]["stage"].unique()
    if len(invalid_stages) > 0:
        issues.append(f"Invalid stage values found: {invalid_stages.tolist()}")

    return issues, stats


def run_validation(jobs_df, sensor_df, verbose=True):
    """
    Run all validation checks. Print a report. Return issues dict.

    Returns:
        dict with keys 'jobs_issues', 'sensor_issues', 'sensor_stats', 'passed'
    """
    if verbose:
        print("=" * 55)
        print("DATA VALIDATION REPORT")
        print("=" * 55)

    job_issues    = validate_jobs(jobs_df)
    sensor_issues, sensor_stats = validate_sensor(sensor_df)

    all_issues = job_issues + sensor_issues
    passed     = len(all_issues) == 0

    if verbose:
        print(f"\nJobs table:    {len(jobs_df)} rows")
        print(f"Sensor table:  {sensor_stats['total_rows']:,} rows")
        print(f"Null fault_type (expected): {sensor_stats['null_fault_type']:,}")
        print(f"Stuck sensor instances:     {sensor_stats['stuck_sensor_instances']}")
        print(f"Jobs with timestamp gaps:   {sensor_stats['jobs_with_gaps']}")

        print(f"\nIssues found: {len(all_issues)}")
        if all_issues:
            for issue in all_issues:
                print(f"  ⚠ {issue}")
        else:
            print("  ✓ All checks passed")

        print(f"\nValidation {'PASSED' if passed else 'FAILED'}")
        print("=" * 55)

    return {
        "jobs_issues":   job_issues,
        "sensor_issues": sensor_issues,
        "sensor_stats":  sensor_stats,
        "passed":        passed,
    }


if __name__ == "__main__":
    jobs_df   = pd.read_csv(STAGING_DIR / "jobs_raw.csv",
                            parse_dates=["start_time", "end_time"])
    sensor_df = pd.read_csv(STAGING_DIR / "sensor_data_raw.csv",
                            parse_dates=["timestamp"])

    run_validation(jobs_df, sensor_df)
