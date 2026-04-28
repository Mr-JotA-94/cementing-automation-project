# =============================================================================
# 07_output/export.py
# =============================================================================
# PURPOSE:
#   Produce the final Power BI-ready CSV files from clean and processed data.
#   These are the files Power BI connects to directly.
#
# WHY A SEPARATE EXPORT SCRIPT?
#   Power BI needs data shaped differently than what analysis needs.
#   Analysis wants raw granularity. Power BI wants pre-joined, pre-labeled,
#   rename-friendly tables with no NULLs and no ambiguous column names.
#   This script bridges that gap without touching any upstream script.
#
# OUTPUT FILES (all in 07_output/):
#   pbi_job_summary.csv    → one row per job, all KPIs, Power BI KPI page
#   pbi_time_series.csv    → sensor readings enriched for charting
#   pbi_alerts.csv         → alert log for alerts dashboard page
#   pbi_stage_summary.csv  → stage-level aggregates for breakdown charts
# =============================================================================

import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, EXPORT_FILES

CLEAN_DIR  = STAGING_DIR.parent
ALERTS_CSV = EXPORT_FILES["alerts"]     # Already produced by detect.py


# =============================================================================
# EXPORT 1: Job Summary — KPI dashboard page
# =============================================================================
def export_job_summary():
    """
    Reads job_summary.csv (produced by kpis.py) and enriches it for Power BI.

    Enrichments:
        - Human-readable success label ("Success" / "Failed")
        - NPT category (None / Low / High)
        - Volume efficiency category for conditional formatting
        - All column names renamed to be Power BI friendly (spaces, title case)
    """
    df = pd.read_csv(CLEAN_DIR / "job_summary.csv")

    # Add human-readable labels — Power BI can use these directly in visuals
    df["Success Label"] = df["job_success"].map({True: "Success", False: "Failed"})

    df["NPT Category"] = pd.cut(
        df["npt_pct"],
        bins    = [-0.1, 0, 10, 100],
        labels  = ["No NPT", "Acceptable", "Excessive"]
    ).astype(str)

    df["Volume Category"] = pd.cut(
        df["volume_efficiency_pct"],
        bins    = [-0.1, 70, 90, 100, 200],
        labels  = ["Critical (<70%)", "Below Target", "On Target", "Over-pumped"]
    ).astype(str)

    df["Stability Category"] = pd.cut(
        df["pressure_stability_idx"],
        bins   = [-0.1, 0.10, 0.15, 1.0],
        labels = ["Stable", "Moderate", "Unstable"]
    ).astype(str)

    # Rename to Power BI friendly names
    df = df.rename(columns={
        "job_id":                  "Job ID",
        "total_duration_min":      "Total Duration (min)",
        "pumping_time_min":        "Pumping Time (min)",
        "npt_time_min":            "NPT (min)",
        "npt_pct":                 "NPT %",
        "avg_pressure_psi":        "Avg Pressure (psi)",
        "max_pressure_psi":        "Max Pressure (psi)",
        "pressure_stability_idx":  "Pressure Stability Index",
        "actual_volume_bbls":      "Actual Volume (bbls)",
        "planned_volume_bbls":     "Planned Volume (bbls)",
        "volume_efficiency_pct":   "Volume Efficiency %",
        "n_pressure_alerts":       "Pressure Alerts",
        "n_flow_alerts":           "Flow Alerts",
        "n_stuck_sensor_alerts":   "Stuck Sensor Alerts",
        "n_total_alerts":          "Total Alerts",
        "job_success":             "Job Success",
        "failure_reason":          "Failure Reason",
    })

    # Fill nulls — Power BI handles empty strings better than NaN
    df["Failure Reason"] = df["Failure Reason"].fillna("N/A")

    path = EXPORT_FILES["job_summary"]
    df.to_csv(path, index=False)
    print(f"  ✓ {path.name}: {len(df)} rows, {len(df.columns)} columns")
    return df


# =============================================================================
# EXPORT 2: Time Series — Operations overview page charts
# =============================================================================
def export_time_series():
    """
    Prepares sensor_data_clean for Power BI time-series charts.

    Key decisions:
        - Includes ALL readings (not just active stages) so charts show
          full job timeline including idle and flush phases
        - Adds job metadata (location) so charts can be filtered by well
        - Adds alert flag so anomaly points can be highlighted on charts
        - Rounds aggressively to keep file size manageable
    """
    sensor_df = pd.read_csv(
        CLEAN_DIR / "sensor_data_clean.csv",
        parse_dates=["timestamp"]
    )
    jobs_df = pd.read_csv(CLEAN_DIR / "jobs_clean.csv")

    # Join location from jobs table
    df = sensor_df.merge(
        jobs_df[["job_id", "location", "planned_volume_bbls"]],
        on  = "job_id",
        how = "left"
    )

    # Add alert flag from alerts CSV if it exists
    if ALERTS_CSV.exists():
        alerts_df = pd.read_csv(ALERTS_CSV, parse_dates=["timestamp"])
        # Create a set of (job_id, timestamp) tuples for fast lookup
        alert_set = set(
            zip(alerts_df["job_id"],
                alerts_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S"))
        )
        df["timestamp_str"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        df["Has Alert"]      = df.apply(
            lambda r: (r["job_id"], r["timestamp_str"]) in alert_set, axis=1
        )
        df = df.drop(columns=["timestamp_str"])
    else:
        df["Has Alert"] = False

    # Stage ordering for Power BI sort
    stage_order = {"idle": 1, "pumping": 2, "displacement": 3, "flush": 4}
    df["Stage Order"] = df["stage"].map(stage_order)

    # Select and rename final columns
    df = df[[
        "timestamp", "job_id", "location", "stage", "Stage Order",
        "pressure_psi", "flow_rate_bpm", "density_ppg",
        "pressure_rolling_mean", "pressure_rolling_std",
        "is_anomaly", "fault_type", "is_stuck_sensor",
        "Has Alert", "is_active_stage",
    ]].rename(columns={
        "timestamp":              "Timestamp",
        "job_id":                 "Job ID",
        "location":               "Well",
        "stage":                  "Stage",
        "pressure_psi":           "Pressure (psi)",
        "flow_rate_bpm":          "Flow Rate (bpm)",
        "density_ppg":            "Density (ppg)",
        "pressure_rolling_mean":  "Pressure Rolling Mean",
        "pressure_rolling_std":   "Pressure Rolling Std",
        "is_anomaly":             "Is Anomaly",
        "fault_type":             "Fault Type",
        "is_stuck_sensor":        "Is Stuck Sensor",
        "is_active_stage":        "Is Active Stage",
    })

    df["Fault Type"] = df["Fault Type"].fillna("none")

    path = EXPORT_FILES["time_series"]
    df.to_csv(path, index=False)
    print(f"  ✓ {path.name}: {len(df):,} rows, {len(df.columns)} columns")
    return df


# =============================================================================
# EXPORT 3: Alerts — Alerts dashboard page
# =============================================================================
def export_alerts():
    """
    Enriches the alerts CSV produced by detect.py for Power BI.
    Adds job metadata and severity ordering for visual sorting.
    """
    if not ALERTS_CSV.exists():
        print("  ⚠ pbi_alerts.csv not found — run detect.py first")
        return pd.DataFrame()

    alerts_df = pd.read_csv(ALERTS_CSV, parse_dates=["timestamp"])
    jobs_df   = pd.read_csv(CLEAN_DIR / "jobs_clean.csv",
                            parse_dates=["start_time"])

    # Join job metadata
    df = alerts_df.merge(
        jobs_df[["job_id", "location", "start_time"]],
        on  = "job_id",
        how = "left"
    )

    # Minutes from job start — useful for timeline charts in Power BI
    df["Minutes from Job Start"] = (
        (df["timestamp"] - df["start_time"])
        .dt.total_seconds() / 60
    ).round(1)

    # Severity ordering for Power BI sort
    df["Severity Order"] = df["severity"].map({"critical": 1, "warning": 2})

    # Human readable alert type labels
    alert_labels = {
        "pressure_spike":  "Pressure Spike",
        "flow_dropout":    "Flow Dropout",
        "stuck_sensor":    "Stuck Sensor",
        "npt_pause":       "NPT Pause",
        "under_pressure":  "Under Pressure",
    }
    df["Alert Label"] = df["alert_type"].map(alert_labels).fillna(df["alert_type"])

    df = df.rename(columns={
        "job_id":          "Job ID",
        "timestamp":       "Timestamp",
        "alert_type":      "Alert Type",
        "severity":        "Severity",
        "sensor_value":    "Sensor Value",
        "threshold_value": "Threshold",
        "stage":           "Stage",
        "description":     "Description",
        "location":        "Well",
    }).drop(columns=["start_time"])

    # Sort: critical first, then by job and time
    df = df.sort_values(
        ["Severity Order", "Job ID", "Timestamp"]
    ).drop(columns=["Severity Order"])

    path = EXPORT_FILES["alerts"]
    df.to_csv(path, index=False)
    print(f"  ✓ {path.name}: {len(df)} rows, {len(df.columns)} columns")
    return df


# =============================================================================
# EXPORT 4: Stage Summary — Breakdown charts
# =============================================================================
def export_stage_summary():
    """
    Aggregates sensor data by job and stage.
    One row per job-stage combination — good for bar/column charts
    comparing stage behaviour across jobs.
    """
    sensor_df = pd.read_csv(CLEAN_DIR / "sensor_data_clean.csv",
                            parse_dates=["timestamp"])
    jobs_df   = pd.read_csv(CLEAN_DIR / "jobs_clean.csv")

    df = (
        sensor_df[~sensor_df["is_stuck_sensor"]]
        .groupby(["job_id", "stage"])
        .agg(
            duration_min       = ("timestamp", "count"),
            avg_pressure       = ("pressure_psi",   "mean"),
            max_pressure       = ("pressure_psi",   "max"),
            avg_flow           = ("flow_rate_bpm",  "mean"),
            avg_density        = ("density_ppg",    "mean"),
            anomaly_readings   = ("is_anomaly",     "sum"),
        )
        .round(2)
        .reset_index()
    )

    # Add stage order
    stage_order = {"idle": 1, "pumping": 2, "displacement": 3, "flush": 4}
    df["Stage Order"] = df["stage"].map(stage_order)

    # Join location
    df = df.merge(jobs_df[["job_id", "location"]], on="job_id", how="left")

    df = df.rename(columns={
        "job_id":           "Job ID",
        "stage":            "Stage",
        "duration_min":     "Duration (min)",
        "avg_pressure":     "Avg Pressure (psi)",
        "max_pressure":     "Max Pressure (psi)",
        "avg_flow":         "Avg Flow (bpm)",
        "avg_density":      "Avg Density (ppg)",
        "anomaly_readings": "Anomaly Readings",
        "location":         "Well",
    })

    df = df.sort_values(["Job ID", "Stage Order"])

    path = EXPORT_FILES["stage_summary"]
    df.to_csv(path, index=False)
    print(f"  ✓ {path.name}: {len(df)} rows, {len(df.columns)} columns")
    return df


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    print("=" * 55)
    print("POWER BI EXPORT")
    print("=" * 55)
    print(f"\nOutput directory: {EXPORT_FILES['job_summary'].parent}\n")

    print("[1/4] Job summary...")
    export_job_summary()

    print("[2/4] Time series...")
    export_time_series()

    print("[3/4] Alerts...")
    export_alerts()

    print("[4/4] Stage summary...")
    export_stage_summary()

    print(f"\n{'=' * 55}")
    print("EXPORT COMPLETE")
    print(f"{'=' * 55}")
    print("\nNext step: Open Power BI Desktop")
    print("  Home → Get Data → Text/CSV")
    print(f"  Navigate to: {EXPORT_FILES['job_summary'].parent}")
    print("  Load all four pbi_*.csv files")
