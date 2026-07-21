#!/usr/bin/env python3
"""Create and seed careintel.db from input CSVs."""

import csv
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), 'careintel.db')
INPUT_DIR = os.path.join(os.path.dirname(__file__), 'input')

DDL = [
    # ── Input tables ──────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS dim_measure (
        measure_key             TEXT PRIMARY KEY,
        measure_code            TEXT NOT NULL,
        measure_name            TEXT,
        measure_type            TEXT,
        star_weight             REAL,
        hedis_domain            TEXT,
        age_gender_eligibility  TEXT,
        clinical_description    TEXT,
        nba_default_playbook    TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS dim_plan_contract (
        plan_key            TEXT PRIMARY KEY,
        contract_id         TEXT,
        plan_name           TEXT,
        region              TEXT,
        segment             TEXT,
        star_rating_current REAL,
        star_rating_target  REAL,
        plan_annual_revenue REAL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS dim_member (
        member_key              TEXT PRIMARY KEY,
        dob_year                INTEGER,
        age_band                TEXT,
        gender                  TEXT,
        language_preference     TEXT,
        digital_literacy_segment TEXT,
        socioeconomic_segment   TEXT,
        pcp_provider_key        TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS dim_member_channel_pref (
        member_key          TEXT PRIMARY KEY,
        email_allowed       TEXT,
        sms_allowed         TEXT,
        call_allowed        TEXT,
        preferred_channel   TEXT,
        do_not_contact_flag TEXT,
        channel_risk_notes  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fact_member_gap (
        member_gap_key                  TEXT PRIMARY KEY,
        member_key                      TEXT,
        measure_key                     TEXT,
        measure_code                    TEXT,
        plan_key                        TEXT,
        measurement_year                INTEGER,
        gap_status                      TEXT,
        gap_open_date                   TEXT,
        gap_close_date                  TEXT,
        days_open                       INTEGER,
        clinical_risk_score             REAL,
        nba_propensity_score            REAL,
        previous_year_gap_flag          TEXT,
        upstream_recommended_channel    TEXT,
        upstream_recommended_incentive  TEXT,
        upstream_recommended_priority   TEXT,
        last_outreach_date              TEXT,
        last_outreach_channel           TEXT,
        is_suppressed                   TEXT
    )""",

    # ── Output tables (start empty) ───────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS fact_nba_claude_decision (
        nba_run_id                  TEXT,
        member_gap_key              TEXT,
        member_key                  TEXT,
        measure_key                 TEXT,
        measure_code                TEXT,
        plan_key                    TEXT,
        measurement_year            INTEGER,
        is_in_selected_opportunity  TEXT,
        cohort_id                   TEXT,
        cohort_name                 TEXT,
        cohort_priority_rank        INTEGER,
        nba_action_type             TEXT,
        final_channel               TEXT,
        final_incentive             TEXT,
        priority_score              REAL,
        sla_days_to_contact         INTEGER,
        expected_gap_closure_lift   REAL,
        reason_codes                TEXT,
        explanation_text            TEXT,
        decision_timestamp          TEXT,
        PRIMARY KEY (nba_run_id, member_gap_key)
    )""",
    """CREATE TABLE IF NOT EXISTS dim_nba_campaign (
        campaign_id         TEXT PRIMARY KEY,
        nba_run_id          TEXT,
        measure_key         TEXT,
        plan_key            TEXT,
        campaign_name       TEXT,
        target_cohort_ids   TEXT,
        channel_strategy    TEXT,
        frequency_plan      TEXT,
        message_template_id TEXT,
        incentive_strategy  TEXT,
        created_timestamp   TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fact_nba_outreach_plan (
        nba_run_id          TEXT,
        contact_id          TEXT PRIMARY KEY,
        member_gap_key      TEXT,
        campaign_id         TEXT,
        channel             TEXT,
        planned_datetime    TEXT,
        message_template_id TEXT,
        incentive_offered   TEXT,
        status              TEXT,
        created_timestamp   TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fact_nba_trace (
        nba_run_id                  TEXT,
        timestamp                   TEXT,
        agent                       TEXT,
        step                        TEXT,
        input_summary               TEXT,
        output_summary              TEXT,
        affected_population_count   INTEGER
    )""",
]

INPUT_TABLES = [
    ('dim_measure',             'dim_measure.csv'),
    ('dim_plan_contract',       'dim_plan_contract.csv'),
    ('dim_member',              'dim_member.csv'),
    ('dim_member_channel_pref', 'dim_member_channel_pref.csv'),
    ('fact_member_gap',         'fact_member_gap.csv'),
]

OUTPUT_TABLES = [
    'fact_nba_claude_decision',
    'dim_nba_campaign',
    'fact_nba_outreach_plan',
    'fact_nba_trace',
]

NEW_TABLES_DDL = [
    """CREATE TABLE IF NOT EXISTS measure_benchmarks (
        measure_key TEXT,
        benchmark_year INTEGER,
        national_avg_rate REAL,
        top_quartile_rate REAL,
        bottom_quartile_rate REAL,
        source TEXT,
        last_updated TEXT,
        PRIMARY KEY (measure_key, benchmark_year)
    )""",
    """CREATE TABLE IF NOT EXISTS outreach_costs (
        cost_id TEXT PRIMARY KEY,
        tier INTEGER,
        channel TEXT,
        base_cost REAL,
        incentive_amount REAL,
        total_cost REAL,
        last_updated TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS closure_rate_assumptions (
        assumption_id TEXT PRIMARY KEY,
        measure_key TEXT,
        tier INTEGER,
        expected_rate REAL,
        basis TEXT,
        last_updated TEXT
    )""",
]


def load_csv(conn, table, csv_path):
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ', '.join('?' * len(cols))
    col_list = ', '.join(cols)
    sql = f'INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})'
    conn.executemany(sql, [[r[c] for c in cols] for r in rows])
    return len(rows)


def recalculate_propensity(conn):
    """Recalculate nba_propensity_score for every fact_member_gap row."""
    gaps = conn.execute("SELECT * FROM fact_member_gap").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM fact_member_gap LIMIT 1").description]
    updated = 0
    for row in gaps:
        gap = dict(zip(cols, row))
        mk = gap["member_key"]

        member_row = conn.execute(
            "SELECT * FROM dim_member WHERE member_key=?", (mk,)
        ).fetchone()
        if not member_row:
            continue
        mcols = [d[0] for d in conn.execute("SELECT * FROM dim_member LIMIT 1").description]
        member = dict(zip(mcols, member_row))

        cp_row = conn.execute(
            "SELECT * FROM dim_member_channel_pref WHERE member_key=?", (mk,)
        ).fetchone()
        cpcols = [d[0] for d in conn.execute("SELECT * FROM dim_member_channel_pref LIMIT 1").description]
        channel_pref = dict(zip(cpcols, cp_row)) if cp_row else {}

        score = _calc_propensity(gap, member, channel_pref)
        conn.execute(
            "UPDATE fact_member_gap SET nba_propensity_score=? WHERE member_gap_key=?",
            (score, gap["member_gap_key"])
        )
        updated += 1
    conn.commit()
    print(f'fact_member_gap: propensity recalculated for {updated} rows')


def _calc_propensity(gap, member, channel_pref):
    score = 0.50

    dig = member.get("digital_literacy_segment", "")
    if dig == "High":   score += 0.15
    elif dig == "Medium": score += 0.08
    elif dig == "Low":  score -= 0.10

    prev = str(gap.get("previous_year_gap_flag", "")).lower()
    if prev == "false": score += 0.10
    elif prev == "true": score -= 0.08

    email_ok = str(channel_pref.get("email_allowed", "")).lower() == "true"
    sms_ok   = str(channel_pref.get("sms_allowed",   "")).lower() == "true"
    channels = sum([email_ok, sms_ok])
    if channels >= 2:  score += 0.05
    elif channels == 1: score -= 0.05

    gs = gap.get("gap_status", "")
    if gs == "Borderline": score += 0.10
    elif gs == "Partial":  score -= 0.05

    if str(channel_pref.get("do_not_contact_flag", "")).lower() == "true":
        score -= 0.08

    ses = member.get("socioeconomic_segment", "")
    if ses == "High": score += 0.08
    elif ses == "Low": score -= 0.05

    mc = gap.get("measure_code", "")
    if mc in ("MAD", "AFV"): score += 0.05
    elif mc in ("COL", "BCS"): score -= 0.05

    return round(max(0.10, min(0.95, score)), 3)


def seed_new_tables(conn):
    today = '2026-07-09'

    # Add extra columns if missing
    for col_sql in [
        "ALTER TABLE dim_plan_contract ADD COLUMN plan_annual_revenue REAL DEFAULT 0",
        "ALTER TABLE dim_plan_contract ADD COLUMN total_members INTEGER DEFAULT 0",
        "ALTER TABLE dim_plan_contract ADD COLUMN plan_pmpm_monthly REAL DEFAULT 1100",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass

    # Set plan-level economics — total_members matches generate_synthetic_data.py
    plan_data = [
        ('P001', 450_000_000, 1500, 1050),
        ('P002', 380_000_000, 1200, 1120),
        ('P003', 280_000_000, 1000,  980),
        ('P004', 520_000_000, 1800, 1180),
        ('P005', 180_000_000,  500,  920),
    ]
    for pk, rev, members, pmpm in plan_data:
        conn.execute(
            "UPDATE dim_plan_contract SET plan_annual_revenue=?, total_members=?, plan_pmpm_monthly=? WHERE plan_key=?",
            (rev, members, pmpm, pk)
        )
    conn.commit()
    print('dim_plan_contract: plan_annual_revenue, total_members, plan_pmpm_monthly set')

    # Seed measure_benchmarks
    bench_data = [
        ('M001', 2026, 0.74, 0.82, 0.65),
        ('M002', 2026, 0.68, 0.78, 0.58),
        ('M003', 2026, 0.72, 0.81, 0.62),
        ('M004', 2026, 0.69, 0.79, 0.58),
        ('M005', 2026, 0.78, 0.86, 0.70),
        ('M006', 2026, 0.65, 0.75, 0.55),
        ('M007', 2026, 0.80, 0.88, 0.72),
    ]
    for row in bench_data:
        conn.execute(
            "INSERT OR REPLACE INTO measure_benchmarks (measure_key, benchmark_year, national_avg_rate, top_quartile_rate, bottom_quartile_rate, source, last_updated) VALUES (?,?,?,?,?,?,?)",
            (*row, 'NCQA HEDIS 2024', today)
        )
    conn.commit()
    print(f'measure_benchmarks: {len(bench_data)} rows seeded')

    # Seed outreach_costs
    cost_data = [
        ('T1_EMAIL',    1, 'EMAIL',    1.50, 0,  1.50),
        ('T1_SMS',      1, 'SMS',      0.50, 0,  0.50),
        ('T2_SMS',      2, 'SMS',      0.50, 15, 15.50),
        ('T2_EMAIL',    2, 'EMAIL',    1.50, 15, 16.50),
        ('T3_CALL',     3, 'CALL',     8.00, 25, 33.00),
        ('T3_WHATSAPP', 3, 'WHATSAPP', 0.10, 25, 25.10),
    ]
    for row in cost_data:
        conn.execute(
            "INSERT OR REPLACE INTO outreach_costs (cost_id, tier, channel, base_cost, incentive_amount, total_cost, last_updated) VALUES (?,?,?,?,?,?,?)",
            (*row, today)
        )
    conn.commit()
    print(f'outreach_costs: {len(cost_data)} rows seeded')

    # Seed closure_rate_assumptions (M001-M007 × tiers 1-3)
    tier_defaults = {1: (0.60, 'industry_default'), 2: (0.35, 'industry_default'), 3: (0.18, 'industry_default')}
    cra_rows = []
    for mk in ['M001', 'M002', 'M003', 'M004', 'M005', 'M006', 'M007']:
        for tier, (rate, basis) in tier_defaults.items():
            assumption_id = f'{mk}_T{tier}'
            conn.execute(
                "INSERT OR REPLACE INTO closure_rate_assumptions (assumption_id, measure_key, tier, expected_rate, basis, last_updated) VALUES (?,?,?,?,?,?)",
                (assumption_id, mk, tier, rate, basis, today)
            )
            cra_rows.append(assumption_id)
    conn.commit()
    print(f'closure_rate_assumptions: {len(cra_rows)} rows seeded')


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')

    for ddl in DDL:
        conn.execute(ddl)
    for ddl in NEW_TABLES_DDL:
        conn.execute(ddl)
    conn.commit()

    for table, filename in INPUT_TABLES:
        path = os.path.join(INPUT_DIR, filename)
        count = load_csv(conn, table, path)
        conn.commit()
        print(f'{table}: {count} rows loaded')

    recalculate_propensity(conn)
    seed_new_tables(conn)

    for table in OUTPUT_TABLES:
        cur = conn.execute(f'SELECT COUNT(*) FROM {table}')
        count = cur.fetchone()[0]
        print(f'{table}: {count} rows (ready)')

    conn.close()


if __name__ == '__main__':
    main()
