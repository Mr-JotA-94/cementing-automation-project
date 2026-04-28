# =============================================================================
# 05_processing/kpis.py
# =============================================================================
# PURPOSE:
#   Apply business logic to feature table to produce final KPIs.
#   This is the "analyst layer" — where domain knowledge becomes numbers.
#
# WHAT THIS PRODUCES:
#   job_summary table — one row per job, all KPIs computed, job scored.
#   This is the PRIMARY table Power BI reads for the KPI dashboard page.
#
# KPIs COMPUTED:
#   - NPT %                    (operational efficiency)
#   - Volume Efficiency %      (execution accuracy)
#   - Pressure Stability Index (job quality)
#   - Job Success flag         (composite score)
#   - Failure reason           (explains why a job failed)
# =============================================================================

import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, KPI_THRESHOLDS, DB_TABLE_SUMMARY
from features import build_feature_table

CLEAN_DIR = STAGING_DIR.parent
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# =============================================================================
# KPI 1: NPT Percentage
# =============================================================================
def compute_npt_pct(features_df):
    """
    NPT % = (npt_min / duration_minutes) × 100

    Interpretation:
        0%   = perfect job, no unplanned stops
        10%  = threshold — above this flags as concerning
        >20% = serious operational issue

    WHY duration_minutes AND NOT active_pumping_min?
        NPT is measured against total job time, not just pumping time.
        That's the industry standard. A 30-minute pause on a 60-minute job
        is 50% NPT — even if pumping itself only took 30 minutes.
    """
    df = features_df.copy()
    df["npt_pct"] = (
        (df["npt_min"] / df["duration_minutes"].replace(0, np.nan)) * 100
    ).round(2).fillna(0)

    return df


# =============================================================================
# KPI 2: Volume Efficiency
# =============================================================================
def compute_volume_efficiency(features_df):
    """
    Volume Efficiency % = (actual_volume / planned_volume) × 100

    Interpretation:
        100% = exactly as planned
        >100% = over-pumped (possible formation loss — costly)
        <90%  = under-pumped (job may not achieve zonal isolation)
        <70%  = serious failure — likely incomplete job

    WHY THIS KPI MATTERS:
        Cement volume directly affects wellbore integrity.
        Too little cement = gas migration risk, regulatory issues.
        Too much cement = wasted cost, potential formation damage.
    """
    df = features_df.copy()
    df["volume_efficiency_pct"] = (
        (df["actual_volume_bbls"] / df["planned_volume_bbls"].replace(0, np.nan)) * 100
    ).round(2).fillna(0)

    return df


# =============================================================================
# KPI 3: Job Success Scoring
# =============================================================================
def compute_job_success(features_df):
    """
    A job is SUCCESS if ALL four conditions are met:
        1. Volume efficiency >= 90%       (enough cement pumped)
        2. NPT % <= 10%                   (minimal downtime)
        3. No pressure spike anomalies    (no critical pressure events)
        4. Pumping stage was completed    (job wasn't aborted)

    If ANY condition fails → job is FAILED.
    failure_reason explains which condition(s) failed.

    WHY A COMPOSITE SCORE (not just one metric)?
        A job can pump 100% of planned volume but still fail —
        if it had a 40% NPT pause, costs ballooned. Both dimensions
        matter. The composite score reflects operational reality.

    DESIGN NOTE:
        We return both the binary flag (job_success) AND the components.
        Power BI shows the flag in summaries, the components in drill-through.
        An ops manager sees "FAILED" at a glance; clicking in reveals why.
    """
    df = features_df.copy()

    vol_threshold  = KPI_THRESHOLDS["volume_efficiency_min_pct"]
    npt_threshold  = KPI_THRESHOLDS["npt_max_pct"]
    max_alerts     = KPI_THRESHOLDS["max_critical_alerts"]

    # Evaluate each condition as a boolean Series
    cond_volume   = df["volume_efficiency_pct"] >= vol_threshold
    cond_npt      = df["npt_pct"]               <= npt_threshold
    cond_pressure = df["pressure_spike"]         <= max_alerts
    cond_pumping  = df["pumping_min"]            >  0              # Pumping happened at all

    df["job_success"] = cond_volume & cond_npt & cond_pressure & cond_pumping

    # Build failure reason string — only populated for failed jobs
    def build_failure_reason(row):
        if row["job_success"]:
            return None

        reasons = []
        if not (row["volume_efficiency_pct"] >= vol_threshold):
            reasons.append(
                f"Volume efficiency {row['volume_efficiency_pct']:.1f}% < {vol_threshold}%"
            )
        if not (row["npt_pct"] <= npt_threshold):
            reasons.append(
                f"NPT {row['npt_pct']:.1f}% > {npt_threshold}%"
            )
        if not (row["pressure_spike"] <= max_alerts):
            reasons.append(
                f"{int(row['pressure_spike'])} pressure spike reading(s)"
            )
        if not (row["pumping_min"] > 0):
            reasons.append("No pumping stage recorded")

        return " | ".join(reasons)

    df["failure_reason"] = df.apply(build_failure_reason, axis=1)

    return df


# =============================================================================
# ASSEMBLE: Build final job_summary table
# =============================================================================
def build_job_summary(features_df):
    """
    Takes the full feature table, applies all KPI computations,
    and selects the final columns for the job_summary table.
    """
    df = features_df.copy()

    # Apply KPI calculations in sequence
    df = compute_npt_pct(df)
    df = compute_volume_efficiency(df)
    df = compute_job_success(df)

    # Select and rename columns to match schema.sql job_summary table
    summary = df[[
        "job_id",
        "duration_minutes",
        "pumping_min",
        "npt_min",
        "npt_pct",
        "avg_pressure_psi",
        "max_pressure_psi",
        "pressure_stability_idx",
        "actual_volume_bbls",
        "planned_volume_bbls",
        "volume_efficiency_pct",
        "pressure_spike",
        "flow_dropout",
        "total_anomaly_readings",
        "job_success",
        "failure_reason",
    ]].copy()

    summary = summary.rename(columns={
        "duration_minutes":      "total_duration_min",
        "pumping_min":           "pumping_time_min",
        "npt_min":               "npt_time_min",        # Match DB column name
        "pressure_spike":        "n_pressure_alerts",
        "flow_dropout":          "n_flow_alerts",
        "total_anomaly_readings":"n_total_alerts",
    })

    # Stuck sensor count — safely get from features, default 0
    summary["n_stuck_sensor_alerts"] = (
        df["stuck_sensor"].fillna(0).astype(int)
        if "stuck_sensor" in df.columns else 0
    )

    # computed_at — timestamp when this summary was generated
    summary["computed_at"] = pd.Timestamp.now()

    # Round all numeric columns cleanly
    numeric_cols = summary.select_dtypes(include=[np.number]).columns
    summary[numeric_cols] = summary[numeric_cols].round(2)

    return summary


# =============================================================================
# SAVE: Write to PostgreSQL and CSV
# =============================================================================
def save_summary(summary_df, engine=None):
    """Save job_summary to both PostgreSQL and CSV for Power BI."""

    # Save to CSV (Power BI export)
    csv_path = CLEAN_DIR / "job_summary.csv"
    summary_df.to_csv(csv_path, index=False)
    print(f"  ✓ Saved CSV: {csv_path.name}")

    # Save to PostgreSQL
    if engine:
        try:
            # Clear existing summary rows before reloading
            with engine.connect() as conn:
                conn.execute(text(f"DELETE FROM job_summary"))
                conn.commit()

            summary_df.to_sql(
                name      = "job_summary",
                con       = engine,
                if_exists = "append",
                index     = False,
                method    = "multi",
            )
            print(f"  ✓ Saved to PostgreSQL: job_summary ({len(summary_df)} rows)")
        except Exception as e:
            print(f"  ⚠ PostgreSQL save failed: {e}")
            print("    CSV was saved — pipeline continues.")


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    print("=" * 55)
    print("KPI COMPUTATION")
    print("=" * 55)

    # Load clean data
    sensor_df = pd.read_csv(CLEAN_DIR / "sensor_data_clean.csv",
                            parse_dates=["timestamp"])
    jobs_df   = pd.read_csv(CLEAN_DIR / "jobs_clean.csv",
                            parse_dates=["start_time", "end_time"])

    # Build features
    print("\n[1/3] Building feature table...")
    features = build_feature_table(sensor_df, jobs_df)

    # Compute KPIs
    print("\n[2/3] Computing KPIs...")
    summary = build_job_summary(features)

    # Connect to DB and save
    print("\n[3/3] Saving results...")
    try:
        host     = os.getenv("DB_HOST", "localhost")
        port     = os.getenv("DB_PORT", "5432")
        name     = os.getenv("DB_NAME", "cementing_db")
        user     = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "")
        engine   = create_engine(
            f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}",
            echo=False
        )
    except Exception:
        engine = None

    save_summary(summary, engine)

    # Print results
    print(f"\n{'=' * 55}")
    print("KPI RESULTS")
    print(f"{'=' * 55}")

    display_cols = ["job_id", "npt_pct", "volume_efficiency_pct",
                    "pressure_stability_idx", "job_success", "failure_reason"]

    pd.set_option("display.max_colwidth", 50)
    pd.set_option("display.width", 120)
    print(summary[display_cols].to_string(index=False))

    # Summary statistics
    n_success = summary["job_success"].sum()
    n_total   = len(summary)
    print(f"\n  Jobs succeeded:   {n_success}/{n_total} "
          f"({n_success/n_total*100:.0f}%)")
    print(f"  Avg NPT %:        {summary['npt_pct'].mean():.1f}%")
    print(f"  Avg Vol Eff %:    {summary['volume_efficiency_pct'].mean():.1f}%")
    print(f"  Avg Stability:    {summary['pressure_stability_idx'].mean():.3f}")
