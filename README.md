# Cementing Operations Data Pipeline
### End-to-End Analytics System | Oil & Gas | Python · SQL · Power BI

---

## What This Project Does

This project simulates, processes, and visualises real-time sensor data from oil well cementing operations — the kind of data produced by SCADA systems at companies like Halliburton and Schlumberger.

It transforms raw time-series sensor readings into operational KPIs and anomaly alerts, delivered through a Power BI decision-support dashboard for two audiences: field engineers reviewing individual jobs and operations managers tracking fleet performance.

---

## Why I Built This

Cementing operations generate continuous streams of pressure, flow rate, and density data that are often reviewed manually and in fragments. This project demonstrates how a structured data pipeline can automate that process — from raw sensor output to actionable insight — using tools standard in enterprise analytics environments.

---

## Pipeline Architecture

```
Raw Simulation (Python)
        ↓
  02_staging/raw/          ← untouched source data
        ↓
ETL Layer (Python)         ← validate → clean → feature engineer
        ↓
PostgreSQL Database        ← structured storage with foreign keys
        ↓
Processing Layer (Python)  ← KPI computation + anomaly detection
        ↓
  07_output/               ← Power BI-ready CSV exports
        ↓
Power BI Dashboard         ← 3-page decision-support report
```

---

## Tech Stack & Decisions

| Tool | Role | Why This Tool |
|---|---|---|
| **Python** | Simulation, ETL, processing, anomaly detection | Complex logic, reproducibility, pandas ecosystem |
| **PostgreSQL** | Structured storage, relational integrity | Industry standard, foreign keys enforce data quality |
| **SQLAlchemy** | Python-to-database bridge | Abstracts raw SQL connections, production-grade |
| **Power BI** | Visualisation layer | Industry standard in O&G operations reporting |

**Why SQL for storage instead of just CSV files?**
Foreign key constraints enforce referential integrity — a sensor reading cannot exist without a valid parent job. This catches data quality issues at insert time, not weeks later during analysis.

**Why Python for KPIs instead of Power Query or DAX?**
Business logic in Python lives in version-controlled scripts with unit-testable functions. DAX and Power Query live inside the .pbix file — harder to review, harder to maintain, invisible to Git.

---

## Key Features

**Realistic Data Simulation**
- 10 cementing jobs with 1-minute sensor intervals
- Four operational stages: idle → pumping → displacement → flush
- Four injected fault types: pressure spikes, flow dropouts, stuck sensors, NPT pauses
- Ground truth file for detection validation

**ETL Layer**
- Pre-cleaning validation with 5 check categories
- Forward-fill null handling per job (not globally)
- Physical limit clipping with domain-appropriate bounds
- Rolling feature engineering (10-minute window)
- Stuck sensor tagging before KPI computation

**KPI Computation**
- NPT % with industry-standard definition (post-pumping idle only)
- Volume efficiency vs planned
- Pressure stability index (coefficient of variation)
- Composite job success scoring with written failure reasons

**Anomaly Detection**
- Statistical: z-score against rolling baseline (stage-aware)
- Rule-based: hard operational thresholds
- Pattern: stuck sensor consecutive-reading detection
- NPT: post-pumping idle period flagging
- Deduplication: critical alerts take priority over warnings

**Power BI Dashboard**
- Page 1: KPI Summary (operations manager view)
- Page 2: Job Detail with time-series pressure/flow charts
- Page 3: Alerts & Issues with timeline scatter

---

## Project Structure

```
cementing-automation/
│
├── config.py                    # All constants — one place to change thresholds
├── main.py                      # Full pipeline orchestrator
│
├── 01_simulation/
│   └── simulate_jobs.py         # SCADA data generator
│
├── 02_staging/
│   └── raw/                     # Generated data (gitignored)
│
├── 03_etl/
│   ├── validate.py              # Pre-cleaning data quality checks
│   └── clean.py                 # Cleaning + feature engineering
│
├── 04_database/
│   ├── schema.sql               # PostgreSQL schema (6 tables)
│   ├── load_data.py             # Staging → database loader
│   └── queries.sql              # Analytical SQL queries
│
├── 05_processing/
│   ├── features.py              # Job-level feature aggregation
│   └── kpis.py                  # KPI computation + job scoring
│
├── 06_anomaly/
│   └── detect.py                # Multi-method anomaly detection
│
├── 07_output/
│   └── export.py                # Power BI CSV export layer
│
└── 08_powerbi/
    └── dashboard_design.md      # Dashboard specification
```

---

## Getting Started

**Prerequisites**
- Python 3.10+
- PostgreSQL 15+
- Power BI Desktop (free)

**Install dependencies**
```bash
pip install pandas numpy sqlalchemy psycopg2-binary python-dotenv
```

**Configure environment**

Create a `.env` file in the project root:
```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=cementing_db
DB_USER=postgres
DB_PASSWORD=your_password
```

**Set up the database**

In pgAdmin or psql, create the database then run the schema:
```bash
psql -U postgres -d cementing_db -f 04_database/schema.sql
```

**Run the full pipeline**
```bash
python main.py
```

Or skip stages if data already exists:
```bash
python main.py --skip-sim    # Use existing raw data
python main.py --skip-db     # CSV outputs only, no database writes
```

**Connect Power BI**

Open Power BI Desktop → Home → Get Data → Text/CSV
Load all four `pbi_*.csv` files from `07_output/`
See `08_powerbi/dashboard_design.md` for relationship setup and visual specs.

---

## Database Schema

Six tables with enforced relationships:

```
jobs (1)
 ├── sensor_data  (many)  — 1-minute SCADA readings
 ├── job_stages   (4)     — stage boundary timestamps
 ├── events       (many)  — operational event log
 ├── job_summary  (1)     — pre-computed KPIs
 └── alerts       (many)  — anomaly detection output
```

---

## KPI Definitions

| KPI | Formula | Threshold |
|---|---|---|
| NPT % | (post-pumping idle min ÷ total job min) × 100 | ≤ 10% |
| Volume Efficiency % | (actual volume ÷ planned volume) × 100 | ≥ 90% |
| Pressure Stability Index | std(pressure) ÷ mean(pressure) during pumping | Lower is better |
| Job Success | NPT ≤ 10% AND Vol Eff ≥ 90% AND zero critical alerts | Boolean |

---

## Extending This Project

**Add real-time ingestion**
Replace `simulate_jobs.py` with a Kafka consumer or MQTT listener.
The ETL layer reads from a stream instead of a CSV — no other changes needed.

**Add ML anomaly detection**
Replace rule-based thresholds in `detect.py` with an Isolation Forest
or LSTM autoencoder trained on the clean sensor data.
Ground truth labels from simulation make this straightforward to validate.

**Deploy to cloud**
- PostgreSQL → Azure Database for PostgreSQL
- Scripts → Azure Data Factory pipelines or Apache Airflow DAGs
- Power BI → Power BI Service with scheduled dataset refresh

---

## Author

Johan Lopez
Data Analyst | Oil & Gas Operations
[LinkedIn](https://linkedin.com) · [GitHub](https://github.com/Mr-JotA-94)
