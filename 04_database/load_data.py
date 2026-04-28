# =============================================================================
# 04_database/load_data.py
# =============================================================================
# PURPOSE:
#   Reads the raw CSVs from 02_staging/raw/ and loads them into PostgreSQL.
#   Also derives and populates the job_stages table from sensor_data.
#
# WHY A SEPARATE LOAD SCRIPT (not just pandas .to_sql)?
#   - We need to derive job_stages from sensor_data (not in the CSV)
#   - We want clear logging of what was loaded and when
#   - We want to handle errors gracefully, not crash silently
#
# RUN ORDER: Always run schema.sql first, then this script.
# =============================================================================

import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import sys
from pathlib import Path

# Allow imports from project root
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import STAGING_DIR, DB_SCHEMA

# Load credentials from .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# =============================================================================
# DATABASE CONNECTION
# =============================================================================
def get_engine():
    """
    Build SQLAlchemy engine from .env credentials.
    SQLAlchemy is the standard Python library for database connections.
    It abstracts the raw psycopg2 driver into a cleaner interface.
    """
    host     = os.getenv("DB_HOST",     "localhost")
    port     = os.getenv("DB_PORT",     "5432")
    name     = os.getenv("DB_NAME",     "cementing_db")
    user     = os.getenv("DB_USER",     "postgres")
    password = os.getenv("DB_PASSWORD", "")

    # Connection string format: dialect+driver://user:password@host:port/database
    connection_string = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

    engine = create_engine(connection_string, echo=False)
    return engine


# =============================================================================
# HELPER: Derive job_stages from sensor_data
# =============================================================================
def derive_job_stages(sensor_df):
    """
    sensor_data has a 'stage' column on every row, but job_stages needs
    one row per stage per job with start/end timestamps.

    This function groups consecutive readings by stage and extracts boundaries.

    Example input (sensor_data rows):
        JOB-001 | 06:00 | idle
        JOB-001 | 06:01 | idle
        JOB-001 | 06:02 | pumping   ← stage changed: idle ended, pumping started
        JOB-001 | 06:03 | pumping

    Example output (job_stages row):
        JOB-001 | idle    | 06:00 | 06:01 | 2 min
        JOB-001 | pumping | 06:02 | 06:03 | 2 min
    """
    stages_rows = []

    for job_id, job_df in sensor_df.groupby("job_id"):
        job_df = job_df.sort_values("timestamp").reset_index(drop=True)

        # Get the unique stages in order they appear
        # We can't just use unique() — we need the FIRST and LAST timestamp per stage
        # because NPT pauses can temporarily change the stage back to 'idle'
        # groupby preserves order, which is what we want here
        stage_groups = (
            job_df.groupby("stage", sort=False)["timestamp"]
            .agg(stage_start="min", stage_end="max")
            .reset_index()
        )

        for _, row in stage_groups.iterrows():
            duration = int(
                (row["stage_end"] - row["stage_start"]).total_seconds() / 60
            ) + 1  # +1 because start and end are both inclusive

            stages_rows.append({
                "job_id":       job_id,
                "stage":        row["stage"],
                "stage_start":  row["stage_start"],
                "stage_end":    row["stage_end"],
                "duration_min": duration,
            })

    return pd.DataFrame(stages_rows)


# =============================================================================
# MAIN LOAD FUNCTION
# =============================================================================
def load_all(engine):
    """
    Loads all tables in the correct dependency order:
    1. jobs          (no dependencies)
    2. sensor_data   (depends on jobs)
    3. job_stages    (derived from sensor_data, depends on jobs)

    Uses if_exists='append' — schema.sql already created the tables.
    We append data, not replace the schema.
    """

    # ------------------------------------------------------------------
    # STEP 1: Load jobs
    # ------------------------------------------------------------------
    print("Loading jobs...")
    jobs_df = pd.read_csv(STAGING_DIR / "jobs_raw.csv", parse_dates=["start_time", "end_time"])

    jobs_df.to_sql(
        name      = "jobs",
        con       = engine,
        if_exists = "append",    # Table already exists from schema.sql — just add rows
        index     = False,       # Don't write the pandas index as a column
        method    = "multi",     # Insert multiple rows per query (faster than row-by-row)
    )
    print(f"  ✓ {len(jobs_df)} jobs loaded")

    # ------------------------------------------------------------------
    # STEP 2: Load sensor_data
    # ------------------------------------------------------------------
    print("Loading sensor_data...")
    sensor_df = pd.read_csv(
        STAGING_DIR / "sensor_data_raw.csv",
        parse_dates=["timestamp"]
    )

    # Replace NaN in fault_type with None (PostgreSQL NULL, not the string "nan")
    sensor_df["fault_type"] = sensor_df["fault_type"].replace({np.nan: None})

    # Round sensor values to match database column precision
    sensor_df["pressure_psi"]  = sensor_df["pressure_psi"].round(2)
    sensor_df["flow_rate_bpm"] = sensor_df["flow_rate_bpm"].round(4)
    sensor_df["density_ppg"]   = sensor_df["density_ppg"].round(4)

    sensor_df.to_sql(
        name      = "sensor_data",
        con       = engine,
        if_exists = "append",
        index     = False,
        method    = "multi",
        chunksize = 500,         # Load 500 rows at a time — prevents memory issues on large datasets
    )
    print(f"  ✓ {len(sensor_df):,} sensor readings loaded")

    # ------------------------------------------------------------------
    # STEP 3: Derive and load job_stages
    # ------------------------------------------------------------------
    print("Deriving job_stages...")
    stages_df = derive_job_stages(sensor_df)

    stages_df.to_sql(
        name      = "job_stages",
        con       = engine,
        if_exists = "append",
        index     = False,
        method    = "multi",
    )
    print(f"  ✓ {len(stages_df)} stage records loaded")

    return jobs_df, sensor_df, stages_df


# =============================================================================
# VERIFICATION: Run quick SQL checks after loading
# =============================================================================
def verify_load(engine):
    """
    Runs sanity-check queries after loading.
    If these pass, the data is in the database and relationships are intact.
    """
    print("\nVerifying load...")

    checks = {
        "Total jobs":           "SELECT COUNT(*) FROM jobs",
        "Total sensor rows":    "SELECT COUNT(*) FROM sensor_data",
        "Total stage records":  "SELECT COUNT(*) FROM job_stages",
        "Jobs with no sensors": """
            SELECT COUNT(*) FROM jobs j
            LEFT JOIN sensor_data s ON j.job_id = s.job_id
            WHERE s.job_id IS NULL
        """,
        "Avg readings per job": """
            SELECT ROUND(AVG(cnt),1) FROM (
                SELECT job_id, COUNT(*) as cnt FROM sensor_data GROUP BY job_id
            ) sub
        """,
    }

    with engine.connect() as conn:
        for label, query in checks.items():
            result = conn.execute(text(query)).scalar()
            print(f"  {label}: {result}")


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("CEMENTING PIPELINE — Database Load")
    print("=" * 60)

    # Confirm staging files exist before attempting connection
    required_files = ["jobs_raw.csv", "sensor_data_raw.csv"]
    for f in required_files:
        path = STAGING_DIR / f
        if not path.exists():
            print(f"\n ERROR: {f} not found in {STAGING_DIR}")
            print("  Run 01_simulation/simulate_jobs.py first.")
            sys.exit(1)

    # Connect
    print("\nConnecting to PostgreSQL...")
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("  ✓ Connection successful")
    except Exception as e:
        print(f"\n  ERROR: Could not connect to database.")
        print(f"  Details: {e}")
        print("  Check your .env file credentials and that PostgreSQL is running.")
        sys.exit(1)

    # Load
    print()
    jobs_df, sensor_df, stages_df = load_all(engine)

    # Verify
    verify_load(engine)

    print(f"\n{'=' * 60}")
    print("DATABASE LOAD COMPLETE")
    print(f"{'=' * 60}")
    print("  Next step: run 03_etl/clean.py")
