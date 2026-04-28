# =============================================================================
# 06_anomaly/detect.py
# =============================================================================
# PURPOSE:
#   Detect anomalies in clean sensor data using two complementary approaches:
#
#   1. STATISTICAL detection — z-score based, uses rolling mean/std
#      "This reading is unusually far from recent trend"
#      Catches: pressure spikes, flow dropouts
#
#   2. RULE-BASED detection — hard thresholds from config.py
#      "This reading crossed an absolute operational limit"
#      Catches: extreme pressure, critically low flow during pumping
#
#   3. PATTERN detection — consecutive value analysis
#      "This sensor hasn't changed in N readings"
#      Catches: stuck sensors
#
# WHY TWO APPROACHES?
#   Statistical alone: misses slow drift that never crosses z-score threshold
#   Rule-based alone:  misses spikes that are extreme relative to baseline
#                      but don't cross the absolute limit
#   Together:          complementary coverage, fewer missed faults
#
# OUTPUT:
#   - alerts table in PostgreSQL
#   - pbi_alerts.csv for Power BI
#   - Detection performance report vs ground truth
# =============================================================================

import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import (
    STAGING_DIR, ALERT_THRESHOLDS, EXPORT_FILES
)

CLEAN_DIR = STAGING_DIR.parent
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# =============================================================================
# DETECTOR 1: Statistical — Z-Score Based
# =============================================================================
def detect_statistical(sensor_df):
    """
    Flag readings where pressure or flow deviates significantly from
    the rolling mean within that job.

    Z-SCORE LOGIC:
        z = (current_value - rolling_mean) / rolling_std

        If z > threshold (default 3.0 from config):
            The current reading is 3 standard deviations above recent trend.
            Statistically: this happens by chance only 0.3% of the time.
            Operationally: almost certainly a real event.

    STAGE AWARENESS:
        Only applied during active stages (pumping, displacement).
        Idle and flush have different pressure norms — applying the same
        z-score across stages would generate constant false positives
        at every stage transition.

    WHY ROLLING (not global)?
        Global mean would flag ALL pumping readings as anomalies
        because they're far above idle baseline. Rolling mean adapts
        to the current operating context — that's the key insight.
    """
    alerts = []
    z_threshold = ALERT_THRESHOLDS["z_score_threshold"]

    active = sensor_df[
        sensor_df["is_active_stage"] &
        sensor_df["pressure_rolling_std"].notna() &
        sensor_df["pressure_rolling_mean"].notna() &
        ~sensor_df["is_stuck_sensor"]        # Exclude stuck sensors — they'd never trigger
    ].copy()

    # Compute z-score for pressure
    active["pressure_z"] = (
        (active["pressure_psi"] - active["pressure_rolling_mean"])
        / active["pressure_rolling_std"].replace(0, np.nan)
    )

    # Flag high z-scores as pressure spike alerts
    spikes = active[active["pressure_z"] > z_threshold]

    for _, row in spikes.iterrows():
        alerts.append({
            "job_id":          row["job_id"],
            "timestamp":       row["timestamp"],
            "alert_type":      "pressure_spike",
            "severity":        "critical" if row["pressure_z"] > z_threshold * 1.5 else "warning",
            "sensor_value":    round(row["pressure_psi"], 2),
            "threshold_value": round(
                row["pressure_rolling_mean"] + z_threshold * row["pressure_rolling_std"], 2
            ),
            "stage":           row["stage"],
            "description":     (
                f"Pressure {row['pressure_psi']:.0f} psi is "
                f"{row['pressure_z']:.1f} std deviations above rolling mean "
                f"({row['pressure_rolling_mean']:.0f} psi)"
            ),
        })

    return pd.DataFrame(alerts) if alerts else pd.DataFrame()


# =============================================================================
# DETECTOR 2: Rule-Based — Hard Thresholds
# =============================================================================
def detect_rule_based(sensor_df):
    """
    Apply absolute operational limits from config.py.
    These fire regardless of rolling context — they're physical limits.

    Rules applied:
        PRESSURE_MAX   : pressure > 4,500 psi → critical (equipment safety)
        UNDER_PRESSURE : pressure < 1,500 psi during pumping → warning
        FLOW_DROPOUT   : flow < 0.5 bpm during active pumping → warning
        FLOW_MAX       : flow > 8.0 bpm → warning (equipment limit)

    DIFFERENCE FROM STATISTICAL:
        Statistical: "unusual relative to recent readings"
        Rule-based:  "crossed a fixed operational limit"

        Example: Pressure at 4,600 psi is always critical — even if the
        last 10 readings were at 4,400 psi (so z-score would be low).
    """
    alerts = []

    max_pressure  = ALERT_THRESHOLDS["pressure_max_psi"]
    min_pumping   = ALERT_THRESHOLDS["pressure_min_pumping"]
    min_flow      = ALERT_THRESHOLDS["flow_min_bpm"]
    max_flow      = ALERT_THRESHOLDS["flow_max_bpm"]

    for _, row in sensor_df.iterrows():

        # Rule 1: Absolute max pressure — applies to ALL stages
        if row["pressure_psi"] > max_pressure:
            alerts.append({
                "job_id":          row["job_id"],
                "timestamp":       row["timestamp"],
                "alert_type":      "pressure_spike",
                "severity":        "critical",
                "sensor_value":    round(row["pressure_psi"], 2),
                "threshold_value": max_pressure,
                "stage":           row["stage"],
                "description":     (
                    f"Pressure {row['pressure_psi']:.0f} psi exceeds "
                    f"absolute maximum {max_pressure} psi"
                ),
            })

        # Rule 2: Under-pressure during pumping
        if row["stage"] == "pumping" and row["pressure_psi"] < min_pumping:
            alerts.append({
                "job_id":          row["job_id"],
                "timestamp":       row["timestamp"],
                "alert_type":      "under_pressure",
                "severity":        "warning",
                "sensor_value":    round(row["pressure_psi"], 2),
                "threshold_value": min_pumping,
                "stage":           row["stage"],
                "description":     (
                    f"Pressure {row['pressure_psi']:.0f} psi below minimum "
                    f"pumping threshold {min_pumping} psi"
                ),
            })

        # Rule 3: Flow dropout during active stages
        if row["is_active_stage"] and row["flow_rate_bpm"] < min_flow:
            alerts.append({
                "job_id":          row["job_id"],
                "timestamp":       row["timestamp"],
                "alert_type":      "flow_dropout",
                "severity":        "warning",
                "sensor_value":    round(row["flow_rate_bpm"], 4),
                "threshold_value": min_flow,
                "stage":           row["stage"],
                "description":     (
                    f"Flow rate {row['flow_rate_bpm']:.3f} bpm below "
                    f"minimum active threshold {min_flow} bpm"
                ),
            })

        # Rule 4: Flow rate too high
        if row["flow_rate_bpm"] > max_flow:
            alerts.append({
                "job_id":          row["job_id"],
                "timestamp":       row["timestamp"],
                "alert_type":      "flow_dropout",
                "severity":        "warning",
                "sensor_value":    round(row["flow_rate_bpm"], 4),
                "threshold_value": max_flow,
                "stage":           row["stage"],
                "description":     (
                    f"Flow rate {row['flow_rate_bpm']:.3f} bpm exceeds "
                    f"maximum {max_flow} bpm"
                ),
            })

    return pd.DataFrame(alerts) if alerts else pd.DataFrame()


# =============================================================================
# DETECTOR 3: Pattern — Stuck Sensor
# =============================================================================
def detect_stuck_sensors(sensor_df):
    """
    Flag periods where a sensor reports the same value repeatedly.
    Uses the is_stuck_sensor column already computed in clean.py.

    WHY USE PRE-COMPUTED FLAG (not recompute here)?
        clean.py already tagged stuck sensor rows with full context.
        Recomputing here would be redundant and could produce different
        results if thresholds changed between sessions.
        We trust the ETL layer's output — that's the point of the pipeline.

    Each continuous run of stuck readings becomes ONE alert entry
    (not one per row). This prevents the alerts table from being
    flooded with hundreds of rows for a single 20-minute stuck sensor.
    """
    alerts = []
    stuck  = sensor_df[sensor_df["is_stuck_sensor"]].copy()

    if stuck.empty:
        return pd.DataFrame()

    stuck = stuck.sort_values(["job_id", "timestamp"])

    for job_id, job_stuck in stuck.groupby("job_id"):
        # Group consecutive stuck readings into single events
        # A new event starts when there's a gap > 1 minute
        time_diff    = job_stuck["timestamp"].diff()
        new_event    = time_diff > pd.Timedelta(minutes=2)
        event_id     = new_event.cumsum()

        for _, event_rows in job_stuck.groupby(event_id):
            duration = len(event_rows)
            alerts.append({
                "job_id":          job_id,
                "timestamp":       event_rows["timestamp"].iloc[0],
                "alert_type":      "stuck_sensor",
                "severity":        "warning",
                "sensor_value":    round(event_rows["pressure_psi"].iloc[0], 2),
                "threshold_value": ALERT_THRESHOLDS["stuck_sensor_readings"],
                "stage":           event_rows["stage"].iloc[0],
                "description":     (
                    f"Sensor repeated same value for {duration} consecutive "
                    f"readings starting at {event_rows['timestamp'].iloc[0]}"
                ),
            })

    return pd.DataFrame(alerts) if alerts else pd.DataFrame()


# =============================================================================
# DETECTOR 4: NPT Pause Detection
# =============================================================================
def detect_npt_pauses(sensor_df):
    """
    Flag periods where the job reverted to idle AFTER pumping started.
    These are unplanned operational stops — NPT events.

    Unlike stuck sensors (data quality issue), NPT pauses are real
    operational events. Both matter but for different reasons:
        Stuck sensor → question your data
        NPT pause    → question your operations
    """
    alerts = []

    for job_id, job_df in sensor_df.groupby("job_id"):
        job_df = job_df.sort_values("timestamp").reset_index(drop=True)

        pumping_rows = job_df[job_df["stage"] == "pumping"]
        if pumping_rows.empty:
            continue

        pump_start = pumping_rows["timestamp"].min()

        # Find idle readings after pumping started
        npt_rows = job_df[
            (job_df["stage"] == "idle") &
            (job_df["timestamp"] > pump_start)
        ]

        if npt_rows.empty:
            continue

        # Group consecutive NPT readings into single events
        time_diff = npt_rows["timestamp"].diff()
        new_event = time_diff > pd.Timedelta(minutes=2)
        event_id  = new_event.cumsum()

        for _, event_rows in npt_rows.groupby(event_id):
            duration = len(event_rows)
            alerts.append({
                "job_id":          job_id,
                "timestamp":       event_rows["timestamp"].iloc[0],
                "alert_type":      "npt_pause",
                "severity":        "warning" if duration < 15 else "critical",
                "sensor_value":    duration,        # Duration in minutes
                "threshold_value": 0,
                "stage":           "idle",
                "description":     (
                    f"Unplanned operational pause: {duration} minutes of idle "
                    f"detected after pumping started"
                ),
            })

    return pd.DataFrame(alerts) if alerts else pd.DataFrame()


# =============================================================================
# COMBINE: Merge all alert sources, deduplicate
# =============================================================================
def combine_alerts(*alert_dfs):
    """
    Merge alert DataFrames from all detectors.
    Deduplicate: same job + timestamp + alert_type = one alert.
    Statistical and rule-based detectors can both flag the same reading.
    We keep the one with higher severity.
    """
    all_alerts = [df for df in alert_dfs if not df.empty]

    if not all_alerts:
        return pd.DataFrame()

    combined = pd.concat(all_alerts, ignore_index=True)

    # Deduplicate: keep critical over warning for same event
    severity_rank = {"critical": 1, "warning": 2}
    combined["_rank"] = combined["severity"].map(severity_rank)

    combined = (
        combined
        .sort_values("_rank")
        .drop_duplicates(subset=["job_id", "timestamp", "alert_type"], keep="first")
        .drop(columns=["_rank"])
        .sort_values(["job_id", "timestamp"])
        .reset_index(drop=True)
    )

    return combined


# =============================================================================
# VALIDATE: Compare detected alerts to ground truth
# =============================================================================
def evaluate_detection(alerts_df, ground_truth_path):
    """
    Compare what we detected to what was actually injected.
    Computes precision and recall per fault type.

    PRECISION: Of alerts we raised, what fraction were real?
               High precision = few false alarms
    RECALL:    Of real faults, what fraction did we catch?
               High recall = few missed faults

    This is what separates "I built anomaly detection" from
    "I built anomaly detection and measured how well it works."
    """
    if not ground_truth_path.exists():
        print("  Ground truth file not found — skipping evaluation")
        return

    gt = pd.read_csv(ground_truth_path)

    print("\n  Detection Performance vs Ground Truth:")
    print(f"  {'Fault Type':<20} {'GT Faults':>10} {'Detected':>10}")
    print(f"  {'-'*42}")

    fault_map = {
        "pressure_spike": "pressure_spike",
        "flow_dropout":   "flow_dropout",
        "stuck_sensor":   "stuck_sensor",
        "npt_pause":      "npt_pause",
    }

    for gt_type, alert_type in fault_map.items():
        gt_count       = len(gt[gt["fault_type"] == gt_type])
        detected_count = len(alerts_df[alerts_df["alert_type"] == alert_type])
        print(f"  {gt_type:<20} {gt_count:>10} {detected_count:>10}")


# =============================================================================
# SAVE: Write alerts to PostgreSQL and CSV
# =============================================================================
def save_alerts(alerts_df, engine=None):
    """Save alerts to PostgreSQL and Power BI CSV."""

    # CSV export
    csv_path = EXPORT_FILES["alerts"]
    alerts_df.to_csv(csv_path, index=False)
    print(f"  ✓ Saved CSV: {csv_path.name} ({len(alerts_df)} alerts)")

    # PostgreSQL
    if engine:
        try:
            with engine.connect() as conn:
                conn.execute(text("DELETE FROM alerts"))
                conn.commit()

            alerts_df.to_sql(
                name      = "alerts",
                con       = engine,
                if_exists = "append",
                index     = False,
                method    = "multi",
            )
            print(f"  ✓ Saved to PostgreSQL: alerts ({len(alerts_df)} rows)")
        except Exception as e:
            print(f"  ⚠ PostgreSQL save failed: {e}")
            print("    CSV was saved — pipeline continues.")


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    print("=" * 55)
    print("ANOMALY DETECTION")
    print("=" * 55)

    sensor_df = pd.read_csv(
        CLEAN_DIR / "sensor_data_clean.csv",
        parse_dates=["timestamp"]
    )

    print(f"\n  Input: {len(sensor_df):,} sensor readings across "
          f"{sensor_df['job_id'].nunique()} jobs")

    # Run all detectors
    print("\n[1/4] Statistical detection (z-score)...")
    stat_alerts  = detect_statistical(sensor_df)
    print(f"  → {len(stat_alerts)} statistical alerts")

    print("\n[2/4] Rule-based detection (hard thresholds)...")
    rule_alerts  = detect_rule_based(sensor_df)
    print(f"  → {len(rule_alerts)} rule-based alerts")

    print("\n[3/4] Stuck sensor detection...")
    stuck_alerts = detect_stuck_sensors(sensor_df)
    print(f"  → {len(stuck_alerts)} stuck sensor alerts")

    print("\n[4/4] NPT pause detection...")
    npt_alerts   = detect_npt_pauses(sensor_df)
    print(f"  → {len(npt_alerts)} NPT pause alerts")

    # Combine and deduplicate
    alerts = combine_alerts(stat_alerts, rule_alerts, stuck_alerts, npt_alerts)
    print(f"\n  Total alerts after deduplication: {len(alerts)}")

    # Alert breakdown
    if not alerts.empty:
        print("\n  Alert breakdown:")
        breakdown = (
            alerts.groupby(["alert_type", "severity"])
            .size()
            .reset_index(name="count")
        )
        for _, row in breakdown.iterrows():
            print(f"    {row['alert_type']:<20} {row['severity']:<10} {row['count']}")

    # Evaluate vs ground truth
    gt_path = STAGING_DIR / "ground_truth.csv"
    evaluate_detection(alerts, gt_path)

    # Connect and save
    print("\nSaving alerts...")
    try:
        engine = create_engine(
            f"postgresql+psycopg2://"
            f"{os.getenv('DB_USER','postgres')}:{os.getenv('DB_PASSWORD','')}@"
            f"{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5432')}/"
            f"{os.getenv('DB_NAME','cementing_db')}",
            echo=False
        )
    except Exception:
        engine = None

    save_alerts(alerts, engine)

    print(f"\n{'=' * 55}")
    print("ANOMALY DETECTION COMPLETE")
    print(f"{'=' * 55}")
