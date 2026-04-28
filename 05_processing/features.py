# =============================================================================
# 05_processing/features.py
# =============================================================================
# PURPOSE:
#   Build job-level feature tables from clean sensor data.
#   These features feed directly into kpis.py.
#
# DIFFERENCE BETWEEN features.py AND kpis.py:
#   features.py  → aggregates raw readings into per-job metrics
#                  (one row per job, summarising sensor behaviour)
#   kpis.py      → applies business logic on top of those metrics
#                  (computes NPT%, success flags, volume efficiency)
#
#   Keeping them separate means: if the business changes a KPI definition,
#   you only touch kpis.py. The underlying feature math stays untouched.
# =============================================================================

import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, ALERT_THRESHOLDS

CLEAN_DIR = STAGING_DIR.parent


# =============================================================================
# FEATURE 1: Pressure features per job
# =============================================================================
def pressure_features(sensor_df):
    """
    Compute pressure statistics per job.
    IMPORTANT: Only uses pumping + displacement stages (is_active_stage = True)
               AND excludes stuck sensor readings.

    Why exclude stuck sensors from stats?
        A stuck sensor at 2,500 psi for 15 readings artificially
        reduces variance — making an unstable job look rock-solid.
        Excluding them gives honest statistics.
    """
    clean = sensor_df[
        sensor_df["is_active_stage"] &
        ~sensor_df["is_stuck_sensor"]
    ].copy()

    features = (
        clean.groupby("job_id")["pressure_psi"]
        .agg(
            avg_pressure_psi   = "mean",
            max_pressure_psi   = "max",
            min_pressure_psi   = "min",
            std_pressure_psi   = "std",
        )
        .round(2)
        .reset_index()
    )

    # Pressure Stability Index = std / mean (coefficient of variation)
    # Lower is better. Close to 0 = very stable. Above 0.15 = investigate.
    features["pressure_stability_idx"] = (
        features["std_pressure_psi"] / features["avg_pressure_psi"]
    ).round(4)

    return features


# =============================================================================
# FEATURE 2: Flow rate features per job
# =============================================================================
def flow_features(sensor_df):
    """
    Compute flow rate statistics during active stages.
    Flow during idle/flush is near-zero by design — not useful for KPIs.
    """
    clean = sensor_df[
        sensor_df["is_active_stage"] &
        ~sensor_df["is_stuck_sensor"]
    ].copy()

    features = (
        clean.groupby("job_id")["flow_rate_bpm"]
        .agg(
            avg_flow_bpm = "mean",
            max_flow_bpm = "max",
            min_flow_bpm = "min",
        )
        .round(4)
        .reset_index()
    )

    # Count readings where flow dropped critically low during active pumping
    # This captures flow_dropout faults that weren't cleaned out
    dropout_threshold = ALERT_THRESHOLDS["flow_min_bpm"]
    flow_dropouts = (
        clean[clean["flow_rate_bpm"] < dropout_threshold]
        .groupby("job_id")
        .size()
        .reset_index(name="flow_dropout_readings")
    )

    features = features.merge(flow_dropouts, on="job_id", how="left")
    features["flow_dropout_readings"] = features["flow_dropout_readings"].fillna(0).astype(int)

    return features


# =============================================================================
# FEATURE 3: Stage duration features per job
# =============================================================================
def stage_duration_features(sensor_df):
    """
    Compute how many minutes were spent in each stage per job.
    Each row = 1 minute (SAMPLE_INTERVAL_S = 60), so COUNT = minutes.

    This also computes NPT minutes — the foundation for NPT%.
    NPT logic: idle readings that appear AFTER pumping has started
    are unplanned downtime.
    """
    # Total readings per stage per job
    stage_counts = (
        sensor_df.groupby(["job_id", "stage"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
    )

    # Ensure all four stage columns exist even if a stage had 0 readings
    for stage in ["idle", "pumping", "displacement", "flush"]:
        if stage not in stage_counts.columns:
            stage_counts[stage] = 0

    stage_counts = stage_counts.rename(columns={
        "idle":         "idle_min",
        "pumping":      "pumping_min",
        "displacement": "displacement_min",
        "flush":        "flush_min",
    })

    # Total active pumping time (pumping + displacement)
    stage_counts["active_pumping_min"] = (
        stage_counts["pumping_min"] + stage_counts["displacement_min"]
    )

    # NPT = idle readings that occurred AFTER first pumping reading
    # These represent unplanned stops, not the normal pre-job idle
    npt_list = []
    for job_id, job_df in sensor_df.groupby("job_id"):
        job_df = job_df.sort_values("timestamp")

        pumping_rows = job_df[job_df["stage"] == "pumping"]
        if pumping_rows.empty:
            npt_list.append({"job_id": job_id, "npt_min": 0})
            continue

        pump_start = pumping_rows["timestamp"].min()

        # Count idle readings that occurred after pumping started
        npt_readings = job_df[
            (job_df["stage"] == "idle") &
            (job_df["timestamp"] > pump_start)
        ]
        npt_list.append({"job_id": job_id, "npt_min": len(npt_readings)})

    npt_df = pd.DataFrame(npt_list)

    stage_counts = stage_counts.merge(npt_df, on="job_id", how="left")
    stage_counts["npt_min"] = stage_counts["npt_min"].fillna(0).astype(int)

    return stage_counts


# =============================================================================
# FEATURE 4: Volume pumped per job
# =============================================================================
def volume_features(sensor_df, jobs_df):
    """
    Compute actual volume pumped and compare to plan.

    FORMULA:
        Volume (barrels) = flow_rate (bpm) × time_interval (minutes)
        Since each reading = 1 minute: SUM(flow_rate) during active stages
        gives total barrels pumped.

    WHY SUM AND NOT MEAN × DURATION?
        Sum handles variable flow rates correctly.
        If flow dropped to zero for 5 minutes (dropout), sum naturally
        captures that lost volume. Mean × duration would not.
    """
    volume = (
        sensor_df[sensor_df["is_active_stage"]]
        .groupby("job_id")["flow_rate_bpm"]
        .sum()
        .round(2)
        .reset_index()
        .rename(columns={"flow_rate_bpm": "actual_volume_bbls"})
    )

    volume = volume.merge(
        jobs_df[["job_id", "planned_volume_bbls"]],
        on="job_id",
        how="left"
    )

    return volume


# =============================================================================
# FEATURE 5: Anomaly counts per job
# =============================================================================
def anomaly_features(sensor_df):
    """
    Count anomaly readings by type per job.
    Used both in KPI scoring and in the alerts dashboard page.
    """
    anomaly_counts = (
        sensor_df[sensor_df["is_anomaly"]]
        .groupby(["job_id", "fault_type"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
    )

    # Ensure all fault type columns exist
    for fault in ["pressure_spike", "flow_dropout", "stuck_sensor", "npt_pause"]:
        col = fault
        if col not in anomaly_counts.columns:
            anomaly_counts[col] = 0

    anomaly_counts["total_anomaly_readings"] = (
        anomaly_counts[["pressure_spike", "flow_dropout",
                         "stuck_sensor", "npt_pause"]].sum(axis=1)
    )

    return anomaly_counts


# =============================================================================
# MASTER: Combine all features into one table
# =============================================================================
def build_feature_table(sensor_df, jobs_df):
    """
    Joins all feature sets into a single wide table.
    One row per job. This is what kpis.py receives as input.
    """
    print("  Building pressure features...")
    pressure  = pressure_features(sensor_df)

    print("  Building flow features...")
    flow      = flow_features(sensor_df)

    print("  Building stage duration features...")
    stages    = stage_duration_features(sensor_df)

    print("  Building volume features...")
    volume    = volume_features(sensor_df, jobs_df)

    print("  Building anomaly features...")
    anomalies = anomaly_features(sensor_df)

    # Start with jobs as the base (LEFT JOIN everything to it)
    # This guarantees all jobs appear even if a feature set has no rows for them
    features = jobs_df[["job_id", "start_time", "end_time",
                         "duration_minutes", "location"]].copy()

    for df in [pressure, flow, stages, volume, anomalies]:
        features = features.merge(df, on="job_id", how="left")

    # Fill any remaining NaN with 0 for numeric columns
    numeric_cols = features.select_dtypes(include=[np.number]).columns
    features[numeric_cols] = features[numeric_cols].fillna(0)

    return features


if __name__ == "__main__":
    print("=" * 55)
    print("FEATURE ENGINEERING")
    print("=" * 55)

    sensor_df = pd.read_csv(CLEAN_DIR / "sensor_data_clean.csv",
                            parse_dates=["timestamp"])
    jobs_df   = pd.read_csv(CLEAN_DIR / "jobs_clean.csv",
                            parse_dates=["start_time", "end_time"])

    print()
    features = build_feature_table(sensor_df, jobs_df)

    print(f"\n  Feature table shape: {features.shape}")
    print(f"  Columns: {features.columns.tolist()}")
    print(f"\n  Preview:")
    print(features[["job_id", "avg_pressure_psi", "pressure_stability_idx",
                     "pumping_min", "npt_min", "actual_volume_bbls",
                     "planned_volume_bbls", "total_anomaly_readings"]].to_string(index=False))
