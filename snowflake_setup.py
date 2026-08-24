"""
snowflake_setup.py — One-time Snowflake setup for CareIntel NBA
===============================================================
Run this ONCE to:
  1. Create the Snowflake database + schema + tables
  2. Upload all realistic synthetic data from input/ CSVs

Prerequisites:
  pip install snowflake-connector-python
  Set all SNOWFLAKE_* env vars (copy from .env.example → .env)

Usage:
  python snowflake_setup.py
  python snowflake_setup.py --dry-run   (show DDL, don't execute)
"""

import os, sys, csv, json, argparse
from pathlib import Path

ROOT = Path(__file__).parent
INPUT = ROOT / "input"

def load_env():
    """Load .env file if present."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

load_env()

def sf_env(key, default=None):
    val = os.getenv(key, default or "")
    return val

def get_conn():
    try:
        import snowflake.connector as sf
    except ImportError:
        print("ERROR: snowflake-connector-python not installed.")
        print("  Run:  pip install snowflake-connector-python")
        sys.exit(1)

    cfg = dict(
        account=sf_env("SNOWFLAKE_ACCOUNT"),
        user=sf_env("SNOWFLAKE_USER"),
        password=sf_env("SNOWFLAKE_PASSWORD"),
        warehouse=sf_env("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    )
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(f'SNOWFLAKE_{k.upper()}' for k in missing)}")
        print("Copy .env.example to .env and fill in your Snowflake credentials.")
        sys.exit(1)

    print(f"Connecting to Snowflake account={cfg['account']} user={cfg['user']}...")
    conn = sf.connect(**cfg)
    return conn

DDL = """
CREATE DATABASE IF NOT EXISTS CAREINTEL;
CREATE SCHEMA IF NOT EXISTS CAREINTEL.NBA;
USE SCHEMA CAREINTEL.NBA;

CREATE TABLE IF NOT EXISTS dim_measure (
    measure_key VARCHAR(20) PRIMARY KEY, measure_code VARCHAR(10),
    measure_name VARCHAR(200), measure_type VARCHAR(50), star_weight FLOAT,
    hedis_domain VARCHAR(100), age_gender_eligibility VARCHAR(200),
    clinical_description VARCHAR(500), nba_default_playbook VARCHAR(100)
);
CREATE TABLE IF NOT EXISTS dim_plan_contract (
    plan_key VARCHAR(20) PRIMARY KEY, contract_id VARCHAR(20),
    plan_name VARCHAR(200), region VARCHAR(50), segment VARCHAR(50),
    star_rating_current FLOAT, star_rating_target FLOAT,
    plan_annual_revenue BIGINT, total_members INTEGER,
    plan_pmpm_monthly FLOAT, source_id VARCHAR(50)
);
CREATE TABLE IF NOT EXISTS dim_member (
    member_key VARCHAR(30) PRIMARY KEY, dob_year INTEGER, age_band VARCHAR(10),
    gender VARCHAR(1), language_preference VARCHAR(50),
    digital_literacy_segment VARCHAR(20), socioeconomic_segment VARCHAR(20),
    pcp_provider_key VARCHAR(20), display_name VARCHAR(200), source_id VARCHAR(50)
);
CREATE TABLE IF NOT EXISTS dim_member_channel_pref (
    member_key VARCHAR(30) PRIMARY KEY, email_allowed VARCHAR(5),
    sms_allowed VARCHAR(5), call_allowed VARCHAR(5),
    preferred_channel VARCHAR(20), do_not_contact_flag VARCHAR(5),
    channel_risk_notes VARCHAR(500)
);
CREATE TABLE IF NOT EXISTS fact_member_gap (
    member_gap_key VARCHAR(60) PRIMARY KEY, member_key VARCHAR(30),
    measure_key VARCHAR(20), measure_code VARCHAR(10), plan_key VARCHAR(20),
    measurement_year INTEGER, gap_status VARCHAR(20), gap_open_date DATE,
    gap_close_date DATE, days_open INTEGER, clinical_risk_score FLOAT,
    nba_propensity_score FLOAT, previous_year_gap_flag INTEGER,
    upstream_recommended_channel VARCHAR(20),
    upstream_recommended_incentive VARCHAR(30),
    upstream_recommended_priority VARCHAR(10),
    last_outreach_date DATE, last_outreach_channel VARCHAR(20),
    is_suppressed VARCHAR(5), source_id VARCHAR(50)
);
CREATE TABLE IF NOT EXISTS plan_population (
    plan_key VARCHAR(20) PRIMARY KEY, total_members INTEGER,
    plan_revenue BIGINT, last_updated DATE
);
CREATE TABLE IF NOT EXISTS fact_nba_claude_decision (
    nba_run_id VARCHAR(30), member_gap_key VARCHAR(60),
    plan_key VARCHAR(20), measure_key VARCHAR(20), nba_action_type VARCHAR(50),
    cohort_id VARCHAR(30), cohort_name VARCHAR(100), cohort_priority_rank INTEGER,
    final_channel VARCHAR(20), final_incentive VARCHAR(30), priority_score FLOAT,
    sla_days_to_contact INTEGER, expected_gap_closure_lift FLOAT,
    reason_codes VARCHAR(500), explanation_text VARCHAR(2000),
    is_in_selected_opportunity INTEGER, created_at TIMESTAMP_NTZ,
    PRIMARY KEY (nba_run_id, member_gap_key)
);
CREATE TABLE IF NOT EXISTS dim_nba_campaign (
    campaign_id VARCHAR(30) PRIMARY KEY, nba_run_id VARCHAR(30),
    plan_key VARCHAR(20), measure_key VARCHAR(20),
    channel_strategy VARCHAR(500), frequency_plan VARCHAR(500),
    incentive_strategy VARCHAR(500), message_template VARCHAR(2000),
    target_cohort_ids VARCHAR(500), created_at TIMESTAMP_NTZ
);
CREATE TABLE IF NOT EXISTS fact_nba_outreach_plan (
    outreach_id VARCHAR(60) PRIMARY KEY, nba_run_id VARCHAR(30),
    campaign_id VARCHAR(30), member_gap_key VARCHAR(60), member_key VARCHAR(30),
    plan_key VARCHAR(20), measure_key VARCHAR(20), channel VARCHAR(20),
    planned_datetime TIMESTAMP_NTZ, message_template VARCHAR(2000),
    incentive_offered VARCHAR(30), status VARCHAR(20), sent_at TIMESTAMP_NTZ,
    response_received VARCHAR(5), notes VARCHAR(500), conversation_state VARCHAR(30),
    outreach_sent_at TIMESTAMP_NTZ, created_at TIMESTAMP_NTZ
);
CREATE TABLE IF NOT EXISTS fact_nba_trace (
    trace_id VARCHAR(60) PRIMARY KEY, nba_run_id VARCHAR(30),
    agent_step VARCHAR(50), agent_mode VARCHAR(50),
    input_summary VARCHAR(2000), output_summary VARCHAR(2000),
    affected_population_count INTEGER, created_at TIMESTAMP_NTZ
);
"""

TABLE_FILES = {
    "dim_measure":             None,             # seeded by api startup
    "dim_plan_contract":       "input/dim_plan_contract.csv",
    "dim_member":              "input/dim_member.csv",
    "dim_member_channel_pref": "input/dim_member_channel_pref.csv",
    "fact_member_gap":         "input/fact_member_gap.csv",
}

PLAN_POP = [
    ("P001", 7800,  91728000, "2026-08-24"),
    ("P002", 3900,  53820000, "2026-08-24"),
    ("P003", 6200,  75888000, "2026-08-24"),
    ("P004", 4700,  54144000, "2026-08-24"),
    ("P005", 3400,  36312000, "2026-08-24"),
]

def run_ddl(cur, dry_run=False):
    print("\n── Creating Snowflake schema…")
    for stmt in DDL.strip().split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        print(f"  {stmt[:70]}…" if len(stmt)>72 else f"  {stmt}")
        if not dry_run:
            cur.execute(stmt)
    print("  Done.")

def upload_csv(cur, table: str, csv_path: str, dry_run: bool):
    path = ROOT / csv_path
    if not path.exists():
        print(f"  SKIP {table}: {csv_path} not found (run gen_data.py first)")
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        print(f"  SKIP {table}: empty CSV")
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    if dry_run:
        print(f"  DRY-RUN {table}: would insert {len(rows):,} rows")
        return len(rows)
    # Batch 5,000 rows at a time
    BATCH = 5000
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        data  = [[r.get(c) for c in cols] for r in batch]
        cur.executemany(f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", data)
        total += len(data)
        print(f"  {table}: {total:,}/{len(rows):,}…", end="\r")
    print(f"  {table}: {total:,} rows loaded    ")
    return total

def main():
    parser = argparse.ArgumentParser(description="Set up Snowflake for CareIntel NBA")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN MODE — no changes will be made\n")
        run_ddl(None, dry_run=True)
        for table, path in TABLE_FILES.items():
            if path:
                csv_path = ROOT / path
                if csv_path.exists():
                    with open(csv_path) as f:
                        n = sum(1 for _ in f) - 1
                    print(f"  DRY-RUN {table}: {n:,} rows from {path}")
        return

    conn = get_conn()
    cur  = conn.cursor()

    run_ddl(cur)

    print("\n── Uploading data…")
    for table, path in TABLE_FILES.items():
        if path:
            upload_csv(cur, table, path, dry_run=False)

    print("\n── Seeding plan_population…")
    for row in PLAN_POP:
        cur.execute(
            "INSERT OR REPLACE INTO plan_population "
            "(plan_key, total_members, plan_revenue, last_updated) VALUES (%s,%s,%s,%s)",
            row
        )
    print(f"  {len(PLAN_POP)} plans")

    conn.commit()
    cur.close()
    conn.close()

    print("\n── Verifying row counts…")
    conn2 = get_conn()
    cur2  = conn2.cursor()
    cur2.execute("USE SCHEMA CAREINTEL.NBA")
    for table in ["dim_member","fact_member_gap","dim_plan_contract","plan_population"]:
        cur2.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur2.fetchone()[0]
        print(f"  {table}: {n:,}")
    cur2.close()
    conn2.close()

    print("\nSnowflake setup complete!")
    print("Next: add DB_MODE=snowflake and SNOWFLAKE_* vars to Render environment.")

if __name__ == "__main__":
    main()
