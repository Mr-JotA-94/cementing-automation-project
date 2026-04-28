# =============================================================================
# main.py — Pipeline Orchestrator
# =============================================================================
# PURPOSE:
#   Run the entire cementing data pipeline from a single command.
#   Each stage is called in dependency order with clear logging.
#
# USAGE:
#   python main.py              → run full pipeline
#   python main.py --skip-sim   → skip simulation (use existing raw data)
#   python main.py --skip-db    → skip database load (ETL + KPIs only)
#
# STAGE ORDER (dependency matters — never reorder):
#   1. Simulate    → generates raw data
#   2. Validate    → checks raw data before touching it
#   3. Clean       → transforms raw into clean
#   4. Load DB     → pushes raw data into PostgreSQL
#   5. KPIs        → computes job summaries, writes to DB
#   6. Detect      → runs anomaly detection, writes alerts to DB
#   7. Export      → produces Power BI CSV files
# =============================================================================

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# Allow all sub-module imports
# Numbered folders (01_simulation etc.) are invalid Python module names.
# We inject each folder directly into sys.path so imports work by filename.
ROOT = Path(__file__).resolve().parent
for folder in ["01_simulation", "03_etl", "04_database",
               "05_processing", "06_anomaly", "07_output"]:
    sys.path.insert(0, str(ROOT / folder))
sys.path.insert(0, str(ROOT))


def print_header(stage_num, total, label):
    print(f"\n{'=' * 60}")
    print(f"  STAGE {stage_num}/{total}: {label}")
    print(f"{'=' * 60}")


def print_duration(start):
    elapsed = time.time() - start
    print(f"  ✓ Completed in {elapsed:.1f}s")


def run_pipeline(skip_sim=False, skip_db=False):
    total_start = time.time()
    total_stages = 7

    print(f"\n{'=' * 60}")
    print(f"  CEMENTING PIPELINE — Full Run")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    # ------------------------------------------------------------------
    # STAGE 1: Simulation
    # ------------------------------------------------------------------
    print_header(1, total_stages, "Data Simulation")
    t = time.time()

    if skip_sim:
        print("  Skipped (--skip-sim flag set)")
        print("  Using existing files in 02_staging/raw/")
    else:
        from config import STAGING_DIR
        required = ["jobs_raw.csv", "sensor_data_raw.csv"]
        missing  = [f for f in required if not (STAGING_DIR / f).exists()]

        if not missing:
            print("  Raw data already exists — skipping simulation")
            print("  Delete 02_staging/raw/ and rerun to regenerate")
        else:
            from simulate_jobs import simulate_all_jobs
            import pandas as pd
            from config import STAGING_DIR

            jobs_df, sensor_df, gt_df = simulate_all_jobs()
            jobs_df.to_csv(STAGING_DIR / "jobs_raw.csv",         index=False)
            sensor_df.to_csv(STAGING_DIR / "sensor_data_raw.csv", index=False)
            gt_df.to_csv(STAGING_DIR / "ground_truth.csv",        index=False)

    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 2: Validation
    # ------------------------------------------------------------------
    print_header(2, total_stages, "Data Validation")
    t = time.time()

    import pandas as pd
    from config import STAGING_DIR
    from validate import run_validation

    jobs_raw   = pd.read_csv(STAGING_DIR / "jobs_raw.csv",
                             parse_dates=["start_time", "end_time"])
    sensor_raw = pd.read_csv(STAGING_DIR / "sensor_data_raw.csv",
                             parse_dates=["timestamp"])

    result = run_validation(jobs_raw, sensor_raw, verbose=True)
    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 3: ETL Cleaning
    # ------------------------------------------------------------------
    print_header(3, total_stages, "ETL Cleaning")
    t = time.time()

    from clean import run_cleaning
    jobs_clean, sensor_clean = run_cleaning(save=True, verbose=False)

    if jobs_clean is None:
        print("  CRITICAL: Cleaning failed — pipeline aborted")
        sys.exit(1)

    print(f"  ✓ Jobs cleaned:   {len(jobs_clean)} rows")
    print(f"  ✓ Sensor cleaned: {len(sensor_clean):,} rows, "
          f"{len(sensor_clean.columns)} columns")
    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 4: Database Load
    # ------------------------------------------------------------------
    print_header(4, total_stages, "Database Load")
    t = time.time()

    if skip_db:
        print("  Skipped (--skip-db flag set)")
    else:
        try:
            from load_data import get_engine, load_all, verify_load
            engine = get_engine()
            load_all(engine)
            verify_load(engine)
        except Exception as e:
            print(f"  ⚠ Database load failed: {e}")
            print("  Continuing — CSV outputs will still be produced")

    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 5: KPI Computation
    # ------------------------------------------------------------------
    print_header(5, total_stages, "KPI Computation")
    t = time.time()

    from config import STAGING_DIR
    CLEAN_DIR = STAGING_DIR.parent

    sensor_clean = pd.read_csv(CLEAN_DIR / "sensor_data_clean.csv",
                               parse_dates=["timestamp"])
    jobs_clean   = pd.read_csv(CLEAN_DIR / "jobs_clean.csv",
                               parse_dates=["start_time", "end_time"])

    from features import build_feature_table
    from kpis     import build_job_summary, save_summary

    features = build_feature_table(sensor_clean, jobs_clean)
    summary  = build_job_summary(features)

    try:
        from load_data import get_engine
        engine = get_engine() if not skip_db else None
    except Exception:
        engine = None

    save_summary(summary, engine)

    n_success = summary["job_success"].sum()
    print(f"  ✓ Jobs scored: {n_success}/{len(summary)} succeeded "
          f"({n_success/len(summary)*100:.0f}%)")
    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 6: Anomaly Detection
    # ------------------------------------------------------------------
    print_header(6, total_stages, "Anomaly Detection")
    t = time.time()

    from detect import (
        detect_statistical, detect_rule_based,
        detect_stuck_sensors, detect_npt_pauses,
        combine_alerts, save_alerts
    )

    stat_alerts  = detect_statistical(sensor_clean)
    rule_alerts  = detect_rule_based(sensor_clean)
    stuck_alerts = detect_stuck_sensors(sensor_clean)
    npt_alerts   = detect_npt_pauses(sensor_clean)
    alerts       = combine_alerts(stat_alerts, rule_alerts, stuck_alerts, npt_alerts)

    save_alerts(alerts, engine if not skip_db else None)
    print(f"  ✓ Alerts generated: {len(alerts)} total")
    print_duration(t)

    # ------------------------------------------------------------------
    # STAGE 7: Power BI Export
    # ------------------------------------------------------------------
    print_header(7, total_stages, "Power BI Export")
    t = time.time()

    from export import (
        export_job_summary, export_time_series,
        export_alerts, export_stage_summary
    )

    export_job_summary()
    export_time_series()
    export_alerts()
    export_stage_summary()
    print_duration(t)

    # ------------------------------------------------------------------
    # DONE
    # ------------------------------------------------------------------
    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 60}")
    print(f"  PIPELINE COMPLETE")
    print(f"  Total time: {total_elapsed:.1f}s")
    print(f"  Finished:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}\n")


# =============================================================================
# ARGUMENT PARSING
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cementing Operations Data Pipeline"
    )
    parser.add_argument(
        "--skip-sim",
        action="store_true",
        help="Skip simulation stage (use existing raw data)"
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip all database operations (CSV outputs only)"
    )
    args = parser.parse_args()

    run_pipeline(skip_sim=args.skip_sim, skip_db=args.skip_db)
