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
        star_rating_target  REAL
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


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')

    for ddl in DDL:
        conn.execute(ddl)
    conn.commit()

    for table, filename in INPUT_TABLES:
        path = os.path.join(INPUT_DIR, filename)
        count = load_csv(conn, table, path)
        conn.commit()
        print(f'{table}: {count} rows loaded')

    for table in OUTPUT_TABLES:
        cur = conn.execute(f'SELECT COUNT(*) FROM {table}')
        count = cur.fetchone()[0]
        print(f'{table}: {count} rows (ready)')

    conn.close()


if __name__ == '__main__':
    main()
