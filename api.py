#!/usr/bin/env python3
"""CareIntel NBA FastAPI — connects to careintel.db."""

import os
import sys
import ssl
import csv
import io
import sqlite3
import threading
import logging
import tempfile
import shutil
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from typing import Any

# Windows Server often has an incomplete CA bundle — disable strict verification for outbound HTTPS
ssl._create_default_https_context = ssl._create_unverified_context

import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic
from pyngrok import ngrok as pyngrok_tunnel
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from twilio.rest import Client as TwilioClient

load_dotenv()

# ── Credentials from .env ─────────────────────────────────────────────────────
TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TEST_SMS      = os.getenv("TEST_SMS_NUMBER")

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db"))

app = FastAPI(title="CareIntel NBA API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows_as_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _ensure_tables():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS whatsapp_conversations (
            conversation_id TEXT PRIMARY KEY,
            member_gap_key  TEXT,
            contact_id      TEXT,
            nba_run_id      TEXT,
            member_phone    TEXT,
            member_key      TEXT,
            measure_name    TEXT,
            conversation_state TEXT DEFAULT 'OUTREACH_SENT',
            appointment_date   TEXT,
            follow_up_sent     INTEGER DEFAULT 0,
            gap_closed         INTEGER DEFAULT 0,
            last_inbound_msg   TEXT,
            created_timestamp  TEXT,
            last_updated       TEXT
        );
        CREATE TABLE IF NOT EXISTS campaign_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            nba_run_id TEXT, campaign_id TEXT, evaluation_date TEXT, evaluation_window INTEGER,
            measure_code TEXT,
            total_members_contacted INTEGER, gaps_closed_actual INTEGER, gaps_closed_expected INTEGER,
            actual_closure_rate REAL, expected_closure_rate REAL, performance_status TEXT,
            stars_impact_actual REAL, stars_impact_projected REAL, executive_summary TEXT,
            created_timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS member_evaluations (
            member_eval_id TEXT PRIMARY KEY, evaluation_id TEXT, nba_run_id TEXT,
            contact_id TEXT, member_gap_key TEXT, member_key TEXT, outreach_sent_date TEXT,
            gap_status_at_evaluation TEXT, days_since_outreach INTEGER, responded INTEGER DEFAULT 0,
            recommended_action TEXT, action_reason TEXT, follow_up_scheduled TEXT, created_timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluation_schedule (
            schedule_id TEXT PRIMARY KEY, nba_run_id TEXT, campaign_id TEXT,
            scheduled_date TEXT, evaluation_window INTEGER, status TEXT DEFAULT 'PENDING',
            created_timestamp TEXT
        );
        """)

_ensure_tables()

active_source_id = "demo"

# Safe schema migrations — ignore errors if column already exists
with get_db() as _mc:
    try:
        _mc.execute("ALTER TABLE campaign_evaluations ADD COLUMN measure_code TEXT")
    except Exception:
        pass
    for _col in [
        "ALTER TABLE dim_plan_contract ADD COLUMN plan_annual_revenue REAL DEFAULT 0",
        "ALTER TABLE dim_plan_contract ADD COLUMN total_members INTEGER DEFAULT 0",
        "ALTER TABLE dim_plan_contract ADD COLUMN plan_pmpm_monthly REAL DEFAULT 1100",
        "ALTER TABLE dim_measure ADD COLUMN clinical_description TEXT DEFAULT ''",
        "ALTER TABLE dim_measure ADD COLUMN age_gender_eligibility TEXT DEFAULT ''",
        "ALTER TABLE dim_measure ADD COLUMN nba_default_playbook TEXT DEFAULT ''",
    ]:
        try:
            _mc.execute(_col)
        except Exception:
            pass
    # Backfill clinical_description from CSV if column is empty (after migration)
    try:
        empty_count = _mc.execute("SELECT COUNT(*) FROM dim_measure WHERE clinical_description IS NULL OR clinical_description=''").fetchone()[0]
        if empty_count > 0:
            import csv as _csv
            _dim_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input", "dim_measure.csv")
            if os.path.exists(_dim_csv):
                with open(_dim_csv, newline='', encoding='utf-8-sig') as _f:
                    for _row in _csv.DictReader(_f):
                        _mc.execute(
                            "UPDATE dim_measure SET clinical_description=?, age_gender_eligibility=?, nba_default_playbook=? WHERE measure_key=?",
                            (_row.get('clinical_description',''), _row.get('age_gender_eligibility',''), _row.get('nba_default_playbook',''), _row.get('measure_key',''))
                        )
    except Exception as _e:
        logging.warning(f"[startup] clinical_description backfill skipped: {_e}")
    # Data sources table and source_id columns
    for _col in [
        "ALTER TABLE fact_member_gap ADD COLUMN source_id TEXT DEFAULT 'demo'",
        "ALTER TABLE dim_member ADD COLUMN source_id TEXT DEFAULT 'demo'",
        "ALTER TABLE dim_plan_contract ADD COLUMN source_id TEXT DEFAULT 'demo'",
    ]:
        try:
            _mc.execute(_col)
        except Exception:
            pass
    try:
        _mc.execute("""
        CREATE TABLE IF NOT EXISTS data_sources (
            source_id TEXT PRIMARY KEY,
            source_name TEXT,
            source_type TEXT,
            file_name TEXT,
            uploaded_at TEXT,
            member_count INTEGER,
            gap_count INTEGER,
            plan_count INTEGER,
            is_active INTEGER DEFAULT 0,
            created_timestamp TEXT
        )""")
    except Exception:
        pass
    _mc.execute("""
        INSERT OR IGNORE INTO data_sources VALUES (
            'demo','CareIntel Demo Database','sqlite','careintel.db',
            datetime('now'),250,0,5,1,datetime('now')
        )""")
    try:
        _mc.execute("""
            UPDATE data_sources SET gap_count = (SELECT COUNT(*) FROM fact_member_gap WHERE source_id = 'demo' OR source_id IS NULL)
            WHERE source_id = 'demo'
        """)
    except Exception:
        pass

# New lookup tables — create if missing, seed if empty
_TODAY = "2026-07-09"

with get_db() as _mc:
    _mc.executescript("""
    CREATE TABLE IF NOT EXISTS measure_benchmarks (
        measure_key TEXT,
        benchmark_year INTEGER,
        national_avg_rate REAL,
        top_quartile_rate REAL,
        bottom_quartile_rate REAL,
        source TEXT,
        last_updated TEXT,
        PRIMARY KEY (measure_key, benchmark_year)
    );
    CREATE TABLE IF NOT EXISTS outreach_costs (
        cost_id TEXT PRIMARY KEY,
        tier INTEGER,
        channel TEXT,
        base_cost REAL,
        incentive_amount REAL,
        total_cost REAL,
        last_updated TEXT
    );
    CREATE TABLE IF NOT EXISTS closure_rate_assumptions (
        assumption_id TEXT PRIMARY KEY,
        measure_key TEXT,
        tier INTEGER,
        expected_rate REAL,
        basis TEXT,
        last_updated TEXT
    );
    """)

    if _mc.execute("SELECT COUNT(*) FROM measure_benchmarks").fetchone()[0] == 0:
        _BENCH_SEED = [
            ('M001',2026,0.74,0.82,0.65),('M002',2026,0.68,0.78,0.58),
            ('M003',2026,0.72,0.81,0.62),('M004',2026,0.69,0.79,0.58),
            ('M005',2026,0.78,0.86,0.70),('M006',2026,0.65,0.75,0.55),
            ('M007',2026,0.80,0.88,0.72),
        ]
        for _r in _BENCH_SEED:
            _mc.execute(
                "INSERT OR REPLACE INTO measure_benchmarks (measure_key,benchmark_year,national_avg_rate,top_quartile_rate,bottom_quartile_rate,source,last_updated) VALUES (?,?,?,?,?,?,?)",
                (*_r, 'NCQA HEDIS 2024', _TODAY)
            )

    if _mc.execute("SELECT COUNT(*) FROM outreach_costs").fetchone()[0] == 0:
        _COST_SEED = [
            ('T1_EMAIL',1,'EMAIL',1.50,0,1.50),('T1_SMS',1,'SMS',0.50,0,0.50),
            ('T2_SMS',2,'SMS',0.50,15,15.50),('T2_EMAIL',2,'EMAIL',1.50,15,16.50),
            ('T3_CALL',3,'CALL',8.00,25,33.00),('T3_WHATSAPP',3,'WHATSAPP',0.10,25,25.10),
        ]
        for _r in _COST_SEED:
            _mc.execute(
                "INSERT OR REPLACE INTO outreach_costs (cost_id,tier,channel,base_cost,incentive_amount,total_cost,last_updated) VALUES (?,?,?,?,?,?,?)",
                (*_r, _TODAY)
            )

    if _mc.execute("SELECT COUNT(*) FROM closure_rate_assumptions").fetchone()[0] == 0:
        for _mk in ['M001','M002','M003','M004','M005','M006','M007']:
            for _tier, _rate in [(1,0.60),(2,0.35),(3,0.18)]:
                _mc.execute(
                    "INSERT OR REPLACE INTO closure_rate_assumptions (assumption_id,measure_key,tier,expected_rate,basis,last_updated) VALUES (?,?,?,?,?,?)",
                    (f'{_mk}_T{_tier}', _mk, _tier, _rate, 'industry_default', _TODAY)
                )


@app.on_event("startup")
async def startup_event():
    # ── Auto-seed database if missing or empty (Render cold start) ────────────
    try:
        needs_seed = True
        if os.path.exists(DB_PATH):
            try:
                import sqlite3 as _sq3
                with _sq3.connect(DB_PATH) as _c:
                    _c.execute("SELECT 1 FROM fact_member_gap LIMIT 1")
                needs_seed = False
            except Exception:
                needs_seed = True
        if needs_seed:
            logging.info("[startup] Database missing or empty — running seed_demo_data.py")
            import subprocess
            seed_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_demo_data.py")
            result = subprocess.run(
                [sys.executable, seed_script],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                logging.info("[startup] Database seeded successfully")
            else:
                logging.error(f"[startup] Seed failed: {result.stderr[-500:]}")
        # Always run seed_expansion.py to ensure plan_population and realistic gaps exist
        try:
            import subprocess as _sp
            expand_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_expansion.py")
            if os.path.exists(expand_script):
                _r = _sp.run([sys.executable, expand_script], capture_output=True, text=True, timeout=180)
                if _r.returncode == 0:
                    logging.info("[startup] seed_expansion.py completed successfully")
                else:
                    logging.error(f"[startup] seed_expansion.py failed: {_r.stderr[-300:]}")
        except Exception as _e:
            logging.error(f"[startup] seed_expansion.py error: {_e}")
    except Exception as e:
        logging.error(f"[startup] Seed error: {e}")

    # ── Post-seed migration: ensure all schema columns exist after seeding ──────
    # Module-level ALTER TABLEs run before seed creates tables on a fresh Render
    # deploy (ephemeral filesystem), so they always fail silently there.
    # Re-running here after seed guarantees tables exist before we alter them.
    _post_seed_migrations = [
        "ALTER TABLE fact_member_gap ADD COLUMN source_id TEXT DEFAULT 'demo'",
        "ALTER TABLE dim_member ADD COLUMN source_id TEXT DEFAULT 'demo'",
        "ALTER TABLE dim_plan_contract ADD COLUMN source_id TEXT DEFAULT 'demo'",
        "ALTER TABLE fact_member_gap ADD COLUMN gap_open_date TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN gap_close_date TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN upstream_recommended_channel TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN upstream_recommended_incentive TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN upstream_recommended_priority TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN last_outreach_date TEXT",
        "ALTER TABLE fact_member_gap ADD COLUMN last_outreach_channel TEXT",
    ]
    try:
        with get_db() as _mc:
            for _col in _post_seed_migrations:
                try:
                    _mc.execute(_col)
                except Exception:
                    pass  # column already exists — expected on second+ deploy
        logging.info("[startup] post-seed schema migration complete")
    except Exception as _me:
        logging.error(f"[startup] post-seed migration error: {_me}")

    # ── ngrok tunnel (local dev only) ─────────────────────────────────────────
    url_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ngrok_url.txt")
    try:
        from pyngrok import conf as pyngrok_conf
        pyngrok_conf.get_default().ngrok_path = r"C:\ngrok\ngrok.exe"
        public_url = pyngrok_tunnel.connect(8000)
        url_str = str(public_url)
        with open(url_file, "w") as f:
            f.write(f"NGROK_PUBLIC_URL={url_str}\n")
            f.write(f"WEBHOOK_URL={url_str}/webhook/whatsapp\n")
        logging.info(f"[ngrok] Public URL: {url_str}")
        logging.info(f"[ngrok] Webhook URL: {url_str}/webhook/whatsapp")
    except Exception as e:
        logging.warning(f"[ngrok] Could not start tunnel: {e}")
        logging.warning("[ngrok] Set NGROK_AUTHTOKEN in .env or run 'ngrok authtoken <token>' to enable")


# ── POST /session/start ───────────────────────────────────────────────────────

@app.get("/dashboard")
def serve_dashboard():
    dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")
    return FileResponse(dashboard_path, media_type="text/html",
                        headers={"Cache-Control":"no-cache, no-store, must-revalidate","Pragma":"no-cache","Expires":"0"})

@app.post("/session/start", status_code=201)
def start_session(body: dict[str, Any] = None):
    run_id = f"RUN_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return {"run_id": run_id}


# ── GET /opportunities ────────────────────────────────────────────────────────

@app.get("/opportunities")
def get_opportunities():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                m.measure_key,
                m.measure_code,
                m.measure_name,
                m.hedis_domain,
                m.star_weight,
                p.plan_key,
                p.plan_name,
                p.region,
                p.segment,
                p.star_rating_current,
                p.star_rating_target,
                COUNT(CASE WHEN LOWER(g.gap_status) IN ('open','borderline')
                           AND LOWER(g.is_suppressed) != 'true' THEN 1 END) AS open_gap_count,
                ROUND(AVG(CASE WHEN LOWER(g.is_suppressed) != 'true'
                               THEN g.nba_propensity_score END), 4)         AS avg_propensity
            FROM fact_member_gap g
            JOIN dim_measure        m ON m.measure_key = g.measure_key
            JOIN dim_plan_contract  p ON p.plan_key    = g.plan_key
            GROUP BY m.measure_key, p.plan_key
            HAVING open_gap_count > 0
        """).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        score = round(d["star_weight"] * d["open_gap_count"] + d["avg_propensity"], 4)
        priority = "High" if score >= 8 else ("Medium" if score >= 4 else "Low")
        results.append({**d, "composite_score": score, "priority": priority})

    results.sort(key=lambda x: x["composite_score"], reverse=True)
    return results


# ── GET /opportunities/financial ─────────────────────────────────────────────

@app.get("/opportunities/financial")
def get_opportunities_financial():
    with get_db() as conn:
        # ── Load benchmarks from DB ────────────────────────────────────
        bench_rows = conn.execute("""
            SELECT m.measure_key, m.measure_code, mb.national_avg_rate,
                   mb.top_quartile_rate, mb.bottom_quartile_rate
            FROM dim_measure m
            JOIN measure_benchmarks mb ON mb.measure_key = m.measure_key
            WHERE mb.benchmark_year = (
                SELECT MAX(benchmark_year) FROM measure_benchmarks WHERE measure_key = m.measure_key
            )
        """).fetchall()
        benchmarks = {r["measure_key"]: dict(r) for r in bench_rows}

        # ── Load costs from DB (cheapest channel per tier) ─────────────
        cost_rows = conn.execute(
            "SELECT tier, MIN(total_cost) AS cost FROM outreach_costs GROUP BY tier"
        ).fetchall()
        costs = {r["tier"]: r["cost"] for r in cost_rows}
        T1_COST = costs.get(1, 0.50)
        T2_COST = costs.get(2, 15.50)
        T3_COST = costs.get(3, 25.10)

        # ── Load closure rate assumptions from DB ──────────────────────
        cra_rows = conn.execute(
            "SELECT measure_key, tier, expected_rate, basis FROM closure_rate_assumptions"
        ).fetchall()
        closure_assumptions = {(r["measure_key"], r["tier"]): dict(r) for r in cra_rows}

        # ── Load historical closure rates ──────────────────────────────
        hist_rows = conn.execute("""
            SELECT g.measure_key,
                   COUNT(*) AS total_outreached,
                   SUM(CASE WHEN LOWER(g.gap_status) = 'closed' THEN 1 ELSE 0 END) AS closed_count
            FROM fact_nba_outreach_plan o
            JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
            WHERE o.status IN ('COMPLETED','SENT','SCHEDULED')
            GROUP BY g.measure_key
        """).fetchall()
        hist_by_mk = {r["measure_key"]: dict(r) for r in hist_rows}

        # ── Eligible / compliant counts from fact_member_gap ──────────
        elig_rows = conn.execute("""
            SELECT measure_key, plan_key,
                   COUNT(DISTINCT member_key) AS eligible_count,
                   COUNT(DISTINCT CASE WHEN LOWER(gap_status) = 'closed' THEN member_key END) AS compliant_count
            FROM fact_member_gap
            GROUP BY measure_key, plan_key
        """).fetchall()
        elig_by_mk_pk = {(r["measure_key"], r["plan_key"]): dict(r) for r in elig_rows}

        # ── Plan-level summary (correct distinct-member counts) ────────
        plan_summary_rows = conn.execute("""
            SELECT plan_key,
                   COUNT(*) AS plan_open_gaps,
                   COUNT(DISTINCT member_key) AS plan_members_at_risk
            FROM fact_member_gap
            WHERE LOWER(gap_status) IN ('open','borderline','partial')
              AND LOWER(is_suppressed) NOT IN ('true','1')
            GROUP BY plan_key
        """).fetchall()
        plan_summary = {r["plan_key"]: dict(r) for r in plan_summary_rows}

        # ── plan_population: true member counts and revenue ────────────
        try:
            pop_rows = conn.execute(
                "SELECT plan_key, total_members, plan_revenue FROM plan_population"
            ).fetchall()
            plan_pop = {r["plan_key"]: dict(r) for r in pop_rows}
        except Exception:
            plan_pop = {}  # seed_expansion.py may not have run yet; fall back to dim_plan_contract

        # ── Main query ─────────────────────────────────────────────────
        rows = conn.execute("""
            SELECT m.measure_key, m.measure_code, m.measure_name, m.star_weight, m.hedis_domain,
                   p.plan_key, p.plan_name, p.region, p.segment,
                   p.plan_annual_revenue, p.total_members, p.plan_pmpm_monthly,
                   p.star_rating_current, p.star_rating_target,
                   COUNT(*) AS eligible_members,
                   SUM(CASE WHEN LOWER(g.gap_status) IN ('open','borderline','partial')
                             AND LOWER(g.is_suppressed)!='true' THEN 1 ELSE 0 END) AS open_gaps,
                   SUM(CASE WHEN LOWER(g.gap_status)='closed' THEN 1 ELSE 0 END) AS closed_gaps,
                   SUM(CASE WHEN g.nba_propensity_score > 0.70
                             AND LOWER(g.gap_status) IN ('open','borderline','partial') THEN 1 ELSE 0 END) AS tier1_count,
                   SUM(CASE WHEN g.nba_propensity_score BETWEEN 0.45 AND 0.70
                             AND LOWER(g.gap_status) IN ('open','borderline','partial') THEN 1 ELSE 0 END) AS tier2_count,
                   SUM(CASE WHEN g.nba_propensity_score < 0.45
                             AND LOWER(g.gap_status) IN ('open','borderline','partial') THEN 1 ELSE 0 END) AS tier3_count
            FROM fact_member_gap g
            JOIN dim_measure       m  ON m.measure_key = g.measure_key
            JOIN dim_plan_contract p  ON p.plan_key    = g.plan_key
            GROUP BY m.measure_key, p.plan_key
            HAVING open_gaps > 0
        """).fetchall()

    # Load any Claude-generated analyses into a lookup dict
    ai_analyses = {}
    try:
        with get_db() as conn2:
            fa_rows = conn2.execute("SELECT * FROM financial_analyses").fetchall()
            for fa in fa_rows:
                key = (fa['measure_key'], fa['plan_key'])
                existing = ai_analyses.get(key)
                if not existing or fa['created_timestamp'] > existing['created_timestamp']:
                    ai_analyses[key] = dict(fa)
    except Exception:
        pass

    results = []
    for r in rows:
        d    = dict(r)
        mk   = d["measure_key"]
        mc   = d["measure_code"]
        pk   = d["plan_key"]
        sw   = float(d["star_weight"] or 1.0)

        # ── Benchmark from DB ──────────────────────────────────────────
        bench = benchmarks.get(mk, {})
        benchmark            = bench.get("national_avg_rate", 0.70)
        top_quartile_rate    = bench.get("top_quartile_rate", 0.80)
        bottom_quartile_rate = bench.get("bottom_quartile_rate", 0.60)

        # ── Eligible / compliant from DB (DISTINCT members — more accurate) ──
        elig_data     = elig_by_mk_pk.get((mk, pk), {})
        elig_distinct = max(int(elig_data.get("eligible_count", 1) or 1), 1)
        compliant_cnt = int(elig_data.get("compliant_count", 0) or 0)
        open_population = float(d.get("open_gaps", 0) or 0)

        # ── Plan economics from plan_population table ──────────────────
        pop_data    = plan_pop.get(pk, {})
        plan_revenue = float(pop_data.get("plan_revenue") or d.get("plan_annual_revenue") or 350_000_000)
        plan_members = int(pop_data.get("total_members") or d.get("total_members") or 500)
        plan_pmpm    = float(d.get("plan_pmpm_monthly") or 1100)
        annual_pmpm  = plan_pmpm * 12

        # ── Eligibility rates by measure code ─────────────────────────
        ELIG_RATES = {
            "BCS": 0.28, "COL": 0.42, "EED": 0.12, "CDC": 0.32,
            "MAD": 0.12, "AFV": 0.75, "SPC": 0.18,
        }
        elig_rate = ELIG_RATES.get(mc, 0.25)

        # STEP 1 — Eligible pool from plan_population × eligibility rate
        eligible_pool = max(round(plan_members * elig_rate), 1)

        # STEP 2 — Compliance rate: closed distinct members / all distinct members
        # Using DISTINCT member counts avoids inflation from expansion rows that
        # cycle the same member_keys, which would make compliance look ~5× lower.
        compliance_rate = round(compliant_cnt / elig_distinct, 4)
        gap_to_benchmark = round(max(0.0, min(benchmark - compliance_rate, 0.40)), 4)

        # STEP 3 — Apply compliance to eligible pool → realistic open gaps
        open_gaps_realistic = round(eligible_pool * (1.0 - compliance_rate))
        open_gaps_db = int(d.get("open_gaps", 0) or 0)
        open_population = float(open_gaps_realistic)

        # STEP 4 — Tier % distribution from DB propensity data, scaled to realistic pop
        t1_db_raw = int(d.get("tier1_count", 0) or 0)
        t2_db_raw = int(d.get("tier2_count", 0) or 0)
        t3_db_raw = int(d.get("tier3_count", 0) or 0)
        total_open_db = max(t1_db_raw + t2_db_raw + t3_db_raw, 1)
        t1_pct = t1_db_raw / total_open_db
        t2_pct = t2_db_raw / total_open_db

        # Clamp each tier so rounding never pushes the residual negative
        t1_db = min(round(open_gaps_realistic * t1_pct), open_gaps_realistic)
        t2_db = min(round(open_gaps_realistic * t2_pct), open_gaps_realistic - t1_db)
        t3_db = max(0, open_gaps_realistic - t1_db - t2_db)

        # STEP 5 — Closure rates
        hist    = hist_by_mk.get(mk, {})
        hist_n  = hist.get("total_outreached", 0) or 0
        if hist_n >= 5:
            # Historical data exists — maintain tier hierarchy around the observed rate
            hist_rate     = round(hist["closed_count"] / hist_n, 4)
            t1_close      = min(0.95, round(hist_rate * 1.50, 4))  # T1: high propensity responds best
            t2_close      = hist_rate                               # T2: at the observed average
            t3_close      = round(hist_rate * 0.55, 4)             # T3: needs high-touch, responds less
            closure_basis = "historical"
        else:
            t1_close      = closure_assumptions.get((mk, 1), {}).get("expected_rate", 0.60)
            t2_close      = closure_assumptions.get((mk, 2), {}).get("expected_rate", 0.35)
            t3_close      = closure_assumptions.get((mk, 3), {}).get("expected_rate", 0.18)
            closure_basis = "assumed"

        t1_closures = round(t1_db * t1_close, 1)
        t2_closures = round(t2_db * t2_close, 1)
        t3_closures = round(t3_db * t3_close, 1)
        total_expected_closures = round(t1_closures + t2_closures + t3_closures, 1)

        # STEP 6 — Stars improvement (denominator = eligible_pool)
        stars_improvement = min(
            (total_expected_closures / max(eligible_pool, 1)) * sw * 0.5,
            sw * 0.10
        )
        stars_improvement  = round(stars_improvement, 6)
        stars_all_closed   = round(min(open_gaps_realistic / max(eligible_pool, 1) * sw * 0.5, sw * 0.50), 4)
        stars_expected     = stars_improvement
        stars_per_gap      = round((1.0 / max(eligible_pool, 1)) * sw * 0.5, 8)

        # STEP 7 — CMS bonus
        cms_bonus = round(stars_improvement * plan_revenue * 0.05)
        cms_bonus_all = round(stars_all_closed * plan_revenue * 0.05)
        cms_bonus_expected = cms_bonus

        # STEP 8 — Outreach cost using REALISTIC tier counts
        tier1_cost = int(round(t1_db * T1_COST))
        tier2_cost = int(round(t2_db * T2_COST))
        tier3_cost = int(round(t3_db * T3_COST))
        total_outreach_cost = tier1_cost + tier2_cost + tier3_cost
        total_cost = total_outreach_cost

        # STEP 9 — Net return and ROI (capped display at 100x)
        net_return = cms_bonus - total_outreach_cost
        roi_ratio_raw = round(net_return / max(total_outreach_cost, 1), 1)
        roi_ratio = min(roi_ratio_raw, 100.0)

        notes = []
        if roi_ratio_raw > 100:
            notes.append("Verify with full plan data")

        # STEP 10 — Confidence based on eligible_pool
        hist_has_data = hist_n >= 5
        if eligible_pool >= 5000 and hist_has_data:
            confidence = "HIGH"
            confidence_description = f"High confidence — estimated {eligible_pool:,} eligible members with historical response data"
        elif eligible_pool < 1000:
            confidence = "LOW"
            confidence_description = f"Low confidence — estimated {eligible_pool:,} eligible members. Projections may not be statistically reliable."
        else:
            confidence = "MEDIUM"
            confidence_description = f"Medium confidence — estimated {eligible_pool:,} eligible members (industry benchmark rates applied)"

        # denominator kept for backward compat
        denominator = float(eligible_pool)
        gap_to_benchmark = round(max(0.0, min(benchmark - compliance_rate, 0.40)), 4)

        results.append({
            "measure_key": mk,
            "measure_code": mc, "measure_name": d["measure_name"],
            "star_weight": sw,  "hedis_domain": d["hedis_domain"],
            "plan_key": pk,     "plan_name": d["plan_name"],
            "region": d["region"], "segment": d["segment"],
            "star_rating_current": d["star_rating_current"],
            "star_rating_target":  d["star_rating_target"],
            "denominator": eligible_pool,
            "eligible_members": eligible_pool,
            "eligible_pool": eligible_pool,
            "open_population": round(open_population),
            "open_gaps_realistic": round(open_gaps_realistic),
            "total_in_db": d.get("eligible_members") or 0,
            "open_gaps": open_gaps_db,
            "closed_gaps": d["closed_gaps"] or 0,
            "compliance_rate": compliance_rate,
            "national_benchmark": benchmark,
            "top_quartile_rate": top_quartile_rate,
            "bottom_quartile_rate": bottom_quartile_rate,
            "gap_to_benchmark": gap_to_benchmark,
            "tier1_count": t1_db, "tier2_count": t2_db, "tier3_count": t3_db,
            "tier1_cost": tier1_cost, "tier2_cost": tier2_cost, "tier3_cost": tier3_cost,
            "total_cost": total_cost,
            "total_outreach_cost": total_outreach_cost,
            "tier1_closures": t1_closures, "tier2_closures": t2_closures, "tier3_closures": t3_closures,
            "total_expected_closures": total_expected_closures,
            "tier1_closure_rate": t1_close, "tier2_closure_rate": t2_close, "tier3_closure_rate": t3_close,
            "tier1_basis": closure_basis, "tier2_basis": closure_basis, "tier3_basis": closure_basis,
            "stars_per_gap": stars_per_gap,
            "stars_all_closed": stars_all_closed,
            "stars_improvement": stars_improvement,
            "stars_expected": stars_expected,
            "plan_revenue": int(plan_revenue),
            "plan_total_members": plan_members,
            "plan_pmpm_monthly": plan_pmpm,
            "annual_pmpm": annual_pmpm,
            "cms_bonus": cms_bonus,
            "cms_bonus_all": cms_bonus_all,
            "cms_bonus_expected": cms_bonus_expected,
            "net_return": net_return,
            "roi_ratio": roi_ratio,
            "roi_ratio_raw": roi_ratio_raw,
            "notes": notes,
            "confidence": confidence,
            "confidence_description": confidence_description,
            # Plan-level summary
            "plan_open_gaps": plan_summary.get(pk, {}).get("plan_open_gaps", open_gaps_db),
            "plan_members_at_risk": plan_summary.get(pk, {}).get("plan_members_at_risk", round(open_population)),
            # AI analysis fields — populated below if fresh Claude analysis exists
            "tier1_definition": "",
            "tier1_closure_rationale": "",
            "tier2_definition": "",
            "tier2_closure_rationale": "",
            "tier3_definition": "",
            "tier3_closure_rationale": "",
            "stars_improvement_rationale": "",
            "return_per_dollar": 0,
            "plain_english_summary": "",
            "key_risks": "",
            "key_opportunities": "",
            "recommended_approach": "",
            "ai_analysis": False,
            "ai_model": "",
            "ai_timestamp": "",
        })

        # Merge Claude-generated analysis if fresh (< 24h)
        fa = ai_analyses.get((mk, pk))
        if fa:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(fa['created_timestamp'])).total_seconds() / 3600
                if age_h < 24:
                    result = results[-1]
                    result['tier1_count'] = fa.get('tier_1_count') or result['tier1_count']
                    result['tier1_closure_rate'] = fa.get('tier_1_closure_rate') or result['tier1_closure_rate']
                    result['tier1_definition'] = fa.get('tier_1_definition', '')
                    result['tier1_closure_rationale'] = fa.get('tier_1_closure_rationale', '')
                    result['tier2_count'] = fa.get('tier_2_count') or result['tier2_count']
                    result['tier2_closure_rate'] = fa.get('tier_2_closure_rate') or result['tier2_closure_rate']
                    result['tier2_definition'] = fa.get('tier_2_definition', '')
                    result['tier2_closure_rationale'] = fa.get('tier_2_closure_rationale', '')
                    result['tier3_count'] = fa.get('tier_3_count') or result['tier3_count']
                    result['tier3_definition'] = fa.get('tier_3_definition', '')
                    result['tier3_closure_rationale'] = fa.get('tier_3_closure_rationale', '')
                    # Scale AI tier counts to the realistic open population so
                    # tier1+tier2+tier3 always sums to open_gaps_realistic.
                    # AI analyzes the small demo dataset; open_gaps_realistic is
                    # projected from real plan population (tens of thousands).
                    _ai_tier_sum = result['tier1_count'] + result['tier2_count'] + result['tier3_count']
                    if _ai_tier_sum > 0:
                        _ror = int(result['open_gaps_realistic'])
                        _p1 = result['tier1_count'] / _ai_tier_sum
                        _p2 = result['tier2_count'] / _ai_tier_sum
                        _t1s = min(round(_ror * _p1), _ror)
                        _t2s = min(round(_ror * _p2), _ror - _t1s)
                        _t3s = max(0, _ror - _t1s - _t2s)
                        result['tier1_count'] = _t1s
                        result['tier2_count'] = _t2s
                        result['tier3_count'] = _t3s
                    # Merge AI-computed closures (guard against negative values)
                    if fa.get('tier_1_expected_closures'):
                        result['tier1_closures'] = max(0, fa['tier_1_expected_closures'])
                    if fa.get('tier_2_expected_closures'):
                        result['tier2_closures'] = max(0, fa['tier_2_expected_closures'])
                    if fa.get('tier_3_expected_closures'):
                        result['tier3_closures'] = max(0, fa['tier_3_expected_closures'])
                    if fa.get('expected_total_closures'):
                        result['total_expected_closures'] = max(0, fa['expected_total_closures'])
                    result['stars_improvement'] = fa.get('stars_improvement') or result['stars_improvement']
                    result['stars_improvement_rationale'] = fa.get('stars_improvement_rationale', '')
                    result['cms_bonus'] = int(fa.get('cms_bonus_impact') or result['cms_bonus'])
                    result['tier3_closure_rate'] = fa.get('tier_3_closure_rate') or result['tier3_closure_rate']
                    # Recompute tier costs from AI-merged counts so all displayed numbers are consistent
                    result['tier1_cost'] = int(round(result['tier1_count'] * T1_COST))
                    result['tier2_cost'] = int(round(result['tier2_count'] * T2_COST))
                    result['tier3_cost'] = int(round(result['tier3_count'] * T3_COST))
                    result['total_outreach_cost'] = result['tier1_cost'] + result['tier2_cost'] + result['tier3_cost']
                    result['total_cost'] = result['total_outreach_cost']
                    result['net_return'] = result['cms_bonus'] - result['total_outreach_cost']
                    result['return_per_dollar'] = fa.get('return_per_dollar') or result.get('roi_ratio', 0)
                    result['confidence'] = fa.get('confidence_level') or result['confidence']
                    result['confidence_description'] = fa.get('confidence_rationale') or result['confidence_description']
                    result['plain_english_summary'] = fa.get('plain_english_summary', '')
                    result['key_risks'] = fa.get('key_risks', '')
                    result['key_opportunities'] = fa.get('key_opportunities', '')
                    result['recommended_approach'] = fa.get('recommended_approach', '')
                    result['ai_analysis'] = True
                    result['ai_model'] = fa.get('claude_model_used', '')
                    result['ai_timestamp'] = fa.get('created_timestamp', '')
            except Exception as e:
                logging.warning(f"[financial] Could not merge AI analysis: {e}")

    results.sort(key=lambda x: x["roi_ratio"], reverse=True)
    return results


# ── AI Financial Analysis job tracker ────────────────────────────────────────
_analysis_jobs = {}  # job_id -> {"status": "running"/"complete"/"error", "results": [...]}


# ── POST /financial/analyze/{measure_key}/{plan_key} ──────────────────────────

@app.post("/financial/analyze/{measure_key}/{plan_key}")
def trigger_analysis(measure_key: str, plan_key: str, force: bool = False):
    # Check cache first — return immediately if fresh result exists
    with get_db() as conn:
        try:
            row = conn.execute(
                "SELECT * FROM financial_analyses WHERE measure_key=? AND plan_key=? ORDER BY created_timestamp DESC LIMIT 1",
                (measure_key, plan_key)
            ).fetchone()
        except Exception:
            row = None

    if row and not force:
        age_hours = (datetime.now() - datetime.fromisoformat(row['created_timestamp'])).total_seconds() / 3600
        if age_hours < 24:
            return {"status": "cached", "analysis": dict(row)}

    # Run async — return job_id immediately so the request doesn't time out
    import uuid
    job_id = f"single_{measure_key}_{plan_key}_{str(uuid.uuid4())[:6]}"
    _analysis_jobs[job_id] = {"status": "running", "measure_key": measure_key, "plan_key": plan_key}

    def run():
        try:
            from financial_analysis_loop import analyze_opportunity
            result = analyze_opportunity(measure_key, plan_key)
            _analysis_jobs[job_id]["status"] = "complete"
            _analysis_jobs[job_id]["analysis"] = result
        except Exception as e:
            logging.error(f"[financial] Single analysis error: {e}")
            _analysis_jobs[job_id]["status"] = "error"
            _analysis_jobs[job_id]["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return {"status": "running", "job_id": job_id}


# ── POST /financial/analyze-all ───────────────────────────────────────────────

@app.post("/financial/analyze-all")
def trigger_analyze_all(source_id: str = "demo"):
    import uuid
    job_id = str(uuid.uuid4())[:8]
    _analysis_jobs[job_id] = {"status": "running", "results": [], "total": 0, "done": 0}

    def run():
        from financial_analysis_loop import analyze_all_opportunities
        results = analyze_all_opportunities(source_id)
        _analysis_jobs[job_id]["results"] = results
        _analysis_jobs[job_id]["status"] = "complete"
        _analysis_jobs[job_id]["done"] = len(results)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return {"job_id": job_id, "status": "running"}


# ── GET /financial/analyze-all/status ────────────────────────────────────────

@app.get("/financial/analyze-all/status")
def analyze_all_status():
    return _analysis_jobs


# ── GET /financial/analysis/{measure_key}/{plan_key} ─────────────────────────

@app.get("/financial/analysis/{measure_key}/{plan_key}")
def get_analysis(measure_key: str, plan_key: str):
    with get_db() as conn:
        try:
            row = conn.execute(
                "SELECT * FROM financial_analyses WHERE measure_key=? AND plan_key=? ORDER BY created_timestamp DESC LIMIT 1",
                (measure_key, plan_key)
            ).fetchone()
        except Exception:
            row = None
    if not row:
        raise HTTPException(status_code=404, detail="No analysis found")
    return dict(row)


# ── GET /financial/analyses ───────────────────────────────────────────────────

@app.get("/financial/analyses")
def get_all_analyses():
    with get_db() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM financial_analyses ORDER BY net_return DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ── GET /data/status ──────────────────────────────────────────────────────────

@app.get("/data/status")
def get_data_status():
    today_str = str(date.today())
    with get_db() as conn:
        members_count = conn.execute("SELECT COUNT(*) FROM dim_member").fetchone()[0]
        gaps_count    = conn.execute("SELECT COUNT(*) FROM fact_member_gap").fetchone()[0]
        plans_count   = conn.execute("SELECT COUNT(*) FROM dim_plan_contract").fetchone()[0]
        runs_count    = conn.execute(
            "SELECT COUNT(DISTINCT nba_run_id) FROM fact_nba_trace"
        ).fetchone()[0]
    return {
        "members": {"count": members_count, "source": "demo", "updated": today_str},
        "gaps":    {"count": gaps_count,    "source": "demo", "updated": today_str},
        "plans":   {"count": plans_count,   "source": "demo", "updated": today_str},
        "runs":    {"count": runs_count},
    }


# ── GET /data/sources ─────────────────────────────────────────────────────────

@app.get("/data/sources")
def get_data_sources():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM data_sources ORDER BY is_active DESC, created_timestamp DESC").fetchall()
    return rows_as_dicts(rows)


# ── POST /data/sources/activate/{source_id} ───────────────────────────────────

@app.post("/data/sources/activate/{source_id}")
def activate_source(source_id: str):
    global active_source_id
    with get_db() as conn:
        conn.execute("UPDATE data_sources SET is_active = 0")
        conn.execute("UPDATE data_sources SET is_active = 1 WHERE source_id = ?", (source_id,))
    active_source_id = source_id
    return {"status": "ok", "active_source_id": source_id}


# ── DELETE /data/sources/{source_id} ─────────────────────────────────────────

@app.delete("/data/sources/{source_id}")
def delete_source(source_id: str):
    global active_source_id
    if source_id == "demo":
        raise HTTPException(status_code=400, detail="Cannot delete demo source")
    with get_db() as conn:
        conn.execute("DELETE FROM fact_member_gap WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM dim_member WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM dim_plan_contract WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM data_sources WHERE source_id = ?", (source_id,))
    if active_source_id == source_id:
        active_source_id = "demo"
        with get_db() as conn:
            conn.execute("UPDATE data_sources SET is_active = 1 WHERE source_id = 'demo'")
    return {"status": "ok"}


# ── GET /data/sources/{source_id}/summary ────────────────────────────────────

@app.get("/data/sources/{source_id}/summary")
def get_source_summary(source_id: str):
    with get_db() as conn:
        member_count = conn.execute(
            "SELECT COUNT(*) FROM dim_member WHERE source_id = ? OR (source_id IS NULL AND ? = 'demo')",
            (source_id, source_id)
        ).fetchone()[0]
        gap_count = conn.execute(
            "SELECT COUNT(*) FROM fact_member_gap WHERE source_id = ? OR (source_id IS NULL AND ? = 'demo')",
            (source_id, source_id)
        ).fetchone()[0]
        plan_count = conn.execute(
            "SELECT COUNT(*) FROM dim_plan_contract WHERE source_id = ? OR (source_id IS NULL AND ? = 'demo')",
            (source_id, source_id)
        ).fetchone()[0]
        open_gap_count = conn.execute(
            "SELECT COUNT(*) FROM fact_member_gap WHERE (source_id = ? OR (source_id IS NULL AND ? = 'demo')) AND LOWER(gap_status) IN ('open','borderline')",
            (source_id, source_id)
        ).fetchone()[0]
        top_measures = conn.execute("""
            SELECT g.measure_key, m.measure_code, m.measure_name, COUNT(*) AS open_gaps
            FROM fact_member_gap g
            LEFT JOIN dim_measure m ON m.measure_key = g.measure_key
            WHERE (g.source_id = ? OR (g.source_id IS NULL AND ? = 'demo'))
              AND LOWER(g.gap_status) IN ('open','borderline')
            GROUP BY g.measure_key
            ORDER BY open_gaps DESC LIMIT 3
        """, (source_id, source_id)).fetchall()
    return {
        "member_count": member_count,
        "gap_count": gap_count,
        "plan_count": plan_count,
        "open_gap_count": open_gap_count,
        "top_measures": rows_as_dicts(top_measures),
    }


# ── GET /data/templates/members ───────────────────────────────────────────────

@app.get("/data/templates/members")
def get_template_members():
    rows = [
        "member_id,date_of_birth,gender,language_preference,digital_literacy_segment,socioeconomic_segment,email_allowed,sms_allowed,call_allowed,preferred_channel,do_not_contact_flag",
        "MEM10001,1948-03-15,F,EN,High,Mid,true,true,true,EMAIL,false",
        "MEM10002,1955-07-22,M,ES,Low,Low,false,false,true,CALL,false",
        "MEM10003,1942-11-08,F,ZH,Medium,Mid,true,false,true,CALL,false",
    ]
    content = "\n".join(rows)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=member_template.csv"}
    )


# ── GET /data/templates/gaps ──────────────────────────────────────────────────

@app.get("/data/templates/gaps")
def get_template_gaps():
    rows = [
        "member_id,measure_code,plan_id,measurement_year,gap_status,gap_open_date,clinical_risk_score,days_open,nba_propensity_score,upstream_recommended_channel,upstream_recommended_incentive",
        "MEM10001,BCS,P001,2026,Open,2026-01-15,0.72,187,0.65,EMAIL,GIFTCARD_25",
        "MEM10002,COL,P002,2026,Borderline,2026-02-01,0.55,170,0.48,SMS,FIT_KIT_MAILER",
        "MEM10003,EED,P001,2026,Open,2026-03-10,0.80,132,0.70,CALL,GIFTCARD_15",
    ]
    content = "\n".join(rows)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gaps_template.csv"}
    )


# ── GET /data/templates/plans ─────────────────────────────────────────────────

@app.get("/data/templates/plans")
def get_template_plans():
    rows = [
        "plan_id,plan_name,region,segment,current_star_rating,target_star_rating,annual_revenue,total_members",
        "P001,Aetna Medicare Choice PPO (Northeast),Northeast,MAPD,3.5,4.0,450000000,45000",
        "P002,Aetna Medicare Premier PPO (Southeast),Southeast,MAPD,4.0,4.5,380000000,38000",
        "P003,Aetna Medicare DSNP Community (Midwest),Midwest,DSNP,3.0,4.0,280000000,28000",
        "P004,UHC Medicare Advantage Value (West),West,MAPD,4.5,5.0,520000000,52000",
        "P005,UHC Medicare Signature PPO (West),West,MAPD,2.5,3.0,210000000,21000",
    ]
    content = "\n".join(rows)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=plans_template.csv"}
    )


# ── Background financial analysis helper ──────────────────────────────────────

def _run_analysis_bg(source_id):
    try:
        from financial_analysis_loop import analyze_all_opportunities
        analyze_all_opportunities(source_id)
    except Exception as e:
        logging.error(f"[financial] Background analysis error: {e}")


# ── POST /data/upload/members ─────────────────────────────────────────────────

REQUIRED_MEMBER_COLS = {
    "member_id", "date_of_birth", "gender", "language_preference",
    "digital_literacy_segment", "socioeconomic_segment"
}

@app.post("/data/upload/members")
async def upload_members(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return JSONResponse(
            status_code=400,
            content={"error": "Excel upload requires additional setup; please use CSV format"}
        )
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = REQUIRED_MEMBER_COLS - headers
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": f"Missing required columns: {sorted(missing)}"}
        )

    imported, skipped, errors = 0, 0, []
    member_rows, channel_rows = [], []

    for i, row in enumerate(reader, start=1):
        mid = (row.get("member_id") or "").strip()
        if not mid:
            skipped += 1
            continue
        mk = f"M_{i:04d}"
        lang = (row.get("language_preference") or "EN").strip().upper()
        dig  = (row.get("digital_literacy_segment") or "Medium").strip()
        ses  = (row.get("socioeconomic_segment") or "Mid").strip()
        gender = (row.get("gender") or "U").strip().upper()
        dob    = (row.get("date_of_birth") or "").strip()
        member_rows.append((mk, "", "", gender, lang, dig, ses, mid))
        email_ok = (row.get("email_allowed") or "false").strip().lower()
        sms_ok   = (row.get("sms_allowed")   or "false").strip().lower()
        call_ok  = (row.get("call_allowed")   or "false").strip().lower()
        pref     = (row.get("preferred_channel") or "CALL").strip().upper()
        dnc      = (row.get("do_not_contact_flag") or "false").strip().lower()
        channel_rows.append((mk, email_ok, sms_ok, call_ok, pref, dnc, ""))
        imported += 1

    with get_db() as conn:
        conn.execute("DELETE FROM dim_member")
        conn.execute("DELETE FROM dim_member_channel_pref")
        conn.executemany(
            "INSERT OR REPLACE INTO dim_member (member_key,plan_key,age_band,gender,language_preference,digital_literacy_segment,socioeconomic_segment,display_name) VALUES (?,?,?,?,?,?,?,?)",
            member_rows
        )
        conn.executemany(
            "INSERT OR REPLACE INTO dim_member_channel_pref (member_key,email_allowed,sms_allowed,call_allowed,preferred_channel,do_not_contact_flag,channel_risk_notes) VALUES (?,?,?,?,?,?,?)",
            channel_rows
        )

    threading.Thread(target=_run_analysis_bg, args=('demo',), daemon=True).start()
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── POST /data/upload/gaps ────────────────────────────────────────────────────

REQUIRED_GAP_COLS = {"member_id", "measure_code", "plan_id", "measurement_year", "gap_status", "gap_open_date", "clinical_risk_score", "days_open"}

@app.post("/data/upload/gaps")
async def upload_gaps(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return JSONResponse(
            status_code=400,
            content={"error": "Excel upload requires additional setup; please use CSV format"}
        )
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = REQUIRED_GAP_COLS - headers
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": f"Missing required columns: {sorted(missing)}"}
        )

    with get_db() as conn:
        measure_map = {r[0]: r[1] for r in conn.execute(
            "SELECT measure_code, measure_key FROM dim_measure"
        ).fetchall()}

    imported, skipped, errors = 0, 0, []
    gap_rows = []

    for i, row in enumerate(reader, start=1):
        mid  = (row.get("member_id") or "").strip()
        mc   = (row.get("measure_code") or "").strip().upper()
        pk   = (row.get("plan_id") or "").strip()
        if not mid or not mc or not pk:
            skipped += 1
            continue
        mk = measure_map.get(mc, "")
        gk = f"G_UPLOAD_{i:06d}"
        status   = (row.get("gap_status") or "Open").strip()
        year     = int(row.get("measurement_year") or 2026)
        days_open = int(row.get("days_open") or 0)
        risk      = float(row.get("clinical_risk_score") or 0.5)
        propensity = float(row.get("nba_propensity_score") or 0.5)
        channel   = (row.get("upstream_recommended_channel") or "").strip()
        incentive = (row.get("upstream_recommended_incentive") or "").strip()
        gap_rows.append((gk, mid, mk, mc, pk, status, year, days_open, propensity, risk, "false", "false"))
        imported += 1

    with get_db() as conn:
        conn.execute("DELETE FROM fact_member_gap")
        conn.executemany(
            """INSERT OR REPLACE INTO fact_member_gap
               (member_gap_key,member_key,measure_key,measure_code,plan_key,gap_status,
                measurement_year,days_open,nba_propensity_score,clinical_risk_score,
                previous_year_gap_flag,is_suppressed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            gap_rows
        )

    threading.Thread(target=_run_analysis_bg, args=('demo',), daemon=True).start()
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── POST /data/upload/plans ───────────────────────────────────────────────────

REQUIRED_PLAN_COLS = {"plan_id", "plan_name", "region", "segment", "current_star_rating", "target_star_rating"}

@app.post("/data/upload/plans")
async def upload_plans(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return JSONResponse(
            status_code=400,
            content={"error": "Excel upload requires additional setup; please use CSV format"}
        )
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = REQUIRED_PLAN_COLS - headers
    if missing:
        return JSONResponse(
            status_code=400,
            content={"error": f"Missing required columns: {sorted(missing)}"}
        )

    imported, skipped, errors = 0, 0, []
    plan_rows, pop_rows = [], []

    for i, row in enumerate(reader, start=1):
        pk = (row.get("plan_id") or "").strip()
        if not pk:
            skipped += 1
            continue
        name    = (row.get("plan_name") or pk).strip()
        region  = (row.get("region") or "").strip()
        segment = (row.get("segment") or "MAPD").strip()
        cur_star = float(row.get("current_star_rating") or 3.0)
        tgt_star = float(row.get("target_star_rating") or 4.0)
        revenue  = int(float(row.get("annual_revenue") or 0))
        members  = int(float(row.get("total_members") or 500))
        plan_rows.append((pk, name, "", region, segment, cur_star, tgt_star, revenue, members, 1100))
        pop_rows.append((pk, members, revenue))
        imported += 1

    today_str = str(date.today())
    with get_db() as conn:
        conn.execute("DELETE FROM dim_plan_contract")
        conn.execute("DELETE FROM plan_population")
        conn.executemany(
            """INSERT OR REPLACE INTO dim_plan_contract
               (plan_key,plan_name,contract_id,region,segment,star_rating_current,star_rating_target,
                plan_annual_revenue,total_members,plan_pmpm_monthly) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            plan_rows
        )
        conn.executemany(
            "INSERT OR REPLACE INTO plan_population (plan_key,total_members,plan_revenue,last_updated) VALUES (?,?,?,?)",
            [(pk, m, r, today_str) for pk, m, r in pop_rows]
        )

    threading.Thread(target=_run_analysis_bg, args=('demo',), daemon=True).start()
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── POST /data/reset ──────────────────────────────────────────────────────────

@app.post("/data/reset")
def data_reset():
    import subprocess as _sp
    base = os.path.dirname(os.path.abspath(__file__))
    for script_name in ("seed_demo_data.py", "seed_expansion.py"):
        script = os.path.join(base, script_name)
        if os.path.exists(script):
            r = _sp.run([sys.executable, script], capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"{script_name} failed: {r.stderr[-300:]}"
                )
    return {"status": "ok", "message": "Demo data restored"}


# ── GET /plans ────────────────────────────────────────────────────────────────

@app.get("/plans")
def get_plans():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM dim_plan_contract").fetchall()
    return rows_as_dicts(rows)


# ── PUT /plans/{plan_key}/revenue ─────────────────────────────────────────────

@app.put("/plans/{plan_key}/revenue")
def update_plan_revenue(plan_key: str, body: dict[str, Any]):
    revenue = body.get("revenue")
    if revenue is None:
        raise HTTPException(status_code=400, detail="Missing 'revenue' field")
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE dim_plan_contract SET plan_annual_revenue = ? WHERE plan_key = ?",
            (float(revenue), plan_key)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Plan {plan_key} not found")
        row = conn.execute("SELECT * FROM dim_plan_contract WHERE plan_key = ?", (plan_key,)).fetchone()
    return dict(row)


# ── GET /benchmarks ───────────────────────────────────────────────────────────

@app.get("/benchmarks")
def get_benchmarks():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT m.measure_code, mb.*
            FROM measure_benchmarks mb
            JOIN dim_measure m ON m.measure_key = mb.measure_key
            ORDER BY mb.measure_key, mb.benchmark_year DESC
        """).fetchall()
    return rows_as_dicts(rows)


# ── PUT /benchmarks/{measure_key} ─────────────────────────────────────────────

@app.put("/benchmarks/{measure_key}")
def update_benchmark(measure_key: str, body: dict[str, Any]):
    benchmark_year      = body.get("benchmark_year", 2026)
    national_avg_rate   = body.get("national_avg_rate")
    top_quartile_rate   = body.get("top_quartile_rate")
    bottom_quartile_rate = body.get("bottom_quartile_rate")
    source              = body.get("source", "NCQA HEDIS 2024")
    last_updated        = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO measure_benchmarks
               (measure_key, benchmark_year, national_avg_rate, top_quartile_rate, bottom_quartile_rate, source, last_updated)
               VALUES (?,?,?,?,?,?,?)""",
            (measure_key, benchmark_year, national_avg_rate, top_quartile_rate, bottom_quartile_rate, source, last_updated)
        )
        row = conn.execute(
            "SELECT m.measure_code, mb.* FROM measure_benchmarks mb JOIN dim_measure m ON m.measure_key = mb.measure_key WHERE mb.measure_key = ? AND mb.benchmark_year = ?",
            (measure_key, benchmark_year)
        ).fetchone()
    return dict(row) if row else {"measure_key": measure_key, "benchmark_year": benchmark_year}


# ── GET /costs ─────────────────────────────────────────────────────────────────

@app.get("/costs")
def get_costs():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM outreach_costs ORDER BY tier, channel").fetchall()
    return rows_as_dicts(rows)


# ── PUT /costs/{cost_id} ──────────────────────────────────────────────────────

@app.put("/costs/{cost_id}")
def update_cost(cost_id: str, body: dict[str, Any]):
    base_cost        = body.get("base_cost")
    incentive_amount = body.get("incentive_amount")
    total_cost       = body.get("total_cost")
    last_updated     = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE outreach_costs SET base_cost = ?, incentive_amount = ?, total_cost = ?, last_updated = ? WHERE cost_id = ?",
            (base_cost, incentive_amount, total_cost, last_updated, cost_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Cost entry {cost_id} not found")
        row = conn.execute("SELECT * FROM outreach_costs WHERE cost_id = ?", (cost_id,)).fetchone()
    return dict(row)


# ── Propensity scoring ────────────────────────────────────────────────────────

def calculate_propensity(gap: dict, member: dict, channel_pref: dict):
    """Return (score, factors) using formula-based propensity signals."""
    score = 0.50
    factors = []

    dig = member.get("digital_literacy_segment", "")
    if dig == "High":
        score += 0.15; factors.append(("digital_literacy_High", +0.15))
    elif dig == "Medium":
        score += 0.08; factors.append(("digital_literacy_Medium", +0.08))
    elif dig == "Low":
        score -= 0.10; factors.append(("digital_literacy_Low", -0.10))

    prev = str(gap.get("previous_year_gap_flag", "")).lower()
    if prev == "false":
        score += 0.10; factors.append(("prev_year_closed", +0.10))
    elif prev == "true":
        score -= 0.08; factors.append(("prev_year_missed", -0.08))

    email_ok = str(channel_pref.get("email_allowed", "")).lower() == "true"
    sms_ok   = str(channel_pref.get("sms_allowed",   "")).lower() == "true"
    channels = sum([email_ok, sms_ok])
    if channels >= 2:
        score += 0.05; factors.append(("multi_channel", +0.05))
    elif channels == 1:
        score -= 0.05; factors.append(("single_channel", -0.05))

    gs = gap.get("gap_status", "")
    if gs == "Borderline":
        score += 0.10; factors.append(("gap_borderline", +0.10))
    elif gs == "Partial":
        score -= 0.05; factors.append(("gap_partial", -0.05))

    if str(channel_pref.get("do_not_contact_flag", "")).lower() == "true":
        score -= 0.08; factors.append(("do_not_contact", -0.08))

    ses = member.get("socioeconomic_segment", "")
    if ses == "High":
        score += 0.08; factors.append(("ses_high", +0.08))
    elif ses == "Low":
        score -= 0.05; factors.append(("ses_low", -0.05))

    mc = gap.get("measure_code", "")
    if mc in ("MAD", "AFV"):
        score += 0.05; factors.append((f"measure_{mc}_easy", +0.05))
    elif mc in ("COL", "BCS"):
        score -= 0.05; factors.append((f"measure_{mc}_hard", -0.05))

    score = round(max(0.10, min(0.95, score)), 3)
    return score, factors


@app.get("/member/{member_key}/propensity")
def get_member_propensity(member_key: str):
    with get_db() as conn:
        member = conn.execute(
            "SELECT * FROM dim_member WHERE member_key = ?", (member_key,)
        ).fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        member = dict(member)

        channel_pref = conn.execute(
            "SELECT * FROM dim_member_channel_pref WHERE member_key = ?", (member_key,)
        ).fetchone()
        channel_pref = dict(channel_pref) if channel_pref else {}

        gaps = conn.execute(
            "SELECT * FROM fact_member_gap WHERE member_key = ?", (member_key,)
        ).fetchall()

    results = []
    for gap in gaps:
        gap = dict(gap)
        score, factors = calculate_propensity(gap, member, channel_pref)
        results.append({
            "member_gap_key": gap["member_gap_key"],
            "measure_code": gap.get("measure_code", ""),
            "gap_status": gap.get("gap_status", ""),
            "propensity_score": score,
            "factors": [{"factor": f[0], "delta": f[1]} for f in factors],
        })

    return {"member_key": member_key, "gaps": results}


# ── GET /members ──────────────────────────────────────────────────────────────

@app.get("/members")
def get_members():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT m.*, c.email_allowed, c.sms_allowed, c.call_allowed,
                   c.preferred_channel, c.do_not_contact_flag, c.channel_risk_notes
            FROM dim_member m
            JOIN dim_member_channel_pref c ON c.member_key = m.member_key
        """).fetchall()
    return rows_as_dicts(rows)


# ── GET /gaps ─────────────────────────────────────────────────────────────────

@app.get("/gaps")
def get_gaps():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM fact_member_gap
            WHERE LOWER(is_suppressed) != 'true'
        """).fetchall()
    return rows_as_dicts(rows)


# ── GET /gaps/{measure_key}/{plan_key} ────────────────────────────────────────

@app.get("/gaps/{measure_key}/{plan_key}")
def get_gaps_by_measure_plan(measure_key: str, plan_key: str):
    """Return up to 250 open/borderline gaps for a measure×plan, joined with member + channel data."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT g.*,
                   mb.age_band, mb.gender, mb.language_preference,
                   mb.digital_literacy_segment, mb.socioeconomic_segment,
                   cp.email_allowed, cp.sms_allowed, cp.call_allowed,
                   cp.preferred_channel, cp.do_not_contact_flag
            FROM fact_member_gap g
            JOIN dim_member mb ON mb.member_key = g.member_key
            LEFT JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
            WHERE g.measure_key = ?
              AND g.plan_key = ?
              AND LOWER(g.gap_status) IN ('open', 'borderline')
              AND LOWER(g.is_suppressed) != 'true'
            ORDER BY g.nba_propensity_score DESC
            LIMIT 250
        """, (measure_key, plan_key)).fetchall()
    return rows_as_dicts(rows)


# ── POST /session/{run_id}/decision ───────────────────────────────────────────

@app.post("/session/{run_id}/decision", status_code=201)
def post_decision(run_id: str, body: dict[str, Any]):
    body["nba_run_id"] = run_id
    cols = list(body.keys())
    sql = f"""INSERT OR REPLACE INTO fact_nba_claude_decision
              ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"""
    with get_db() as conn:
        conn.execute(sql, list(body.values()))
    return {"status": "ok", "run_id": run_id}


# ── POST /session/{run_id}/campaign ───────────────────────────────────────────

@app.post("/session/{run_id}/campaign", status_code=201)
def post_campaign(run_id: str, body: dict[str, Any]):
    body["nba_run_id"] = run_id
    cols = list(body.keys())
    sql = f"""INSERT OR REPLACE INTO dim_nba_campaign
              ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"""
    with get_db() as conn:
        conn.execute(sql, list(body.values()))
    return {"status": "ok", "run_id": run_id}


# ── POST /session/{run_id}/outreach ───────────────────────────────────────────

@app.post("/session/{run_id}/outreach", status_code=201)
def post_outreach(run_id: str, body: list[dict[str, Any]]):
    if not body:
        return {"status": "ok", "inserted": 0}
    for row in body:
        row["nba_run_id"] = run_id
    cols = list(body[0].keys())
    sql = f"""INSERT OR REPLACE INTO fact_nba_outreach_plan
              ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"""
    with get_db() as conn:
        conn.executemany(sql, [[r[c] for c in cols] for r in body])
    return {"status": "ok", "inserted": len(body)}


# ── POST /session/{run_id}/trace ──────────────────────────────────────────────

@app.post("/session/{run_id}/trace", status_code=201)
def post_trace(run_id: str, body: dict[str, Any]):
    body["nba_run_id"] = run_id
    cols = list(body.keys())
    sql = f"""INSERT INTO fact_nba_trace
              ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"""
    with get_db() as conn:
        conn.execute(sql, list(body.values()))
    return {"status": "ok", "run_id": run_id}


# ── GET /sessions ─────────────────────────────────────────────────────────────

@app.get("/sessions")
def list_sessions():
    with get_db() as conn:
        run_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT nba_run_id FROM fact_nba_trace ORDER BY timestamp DESC"
        ).fetchall()]
        sessions = []
        for run_id in run_ids:
            opp = conn.execute(
                "SELECT output_summary FROM fact_nba_trace WHERE nba_run_id=? AND step='OPPORTUNITY_SELECTED'",
                (run_id,)
            ).fetchone()
            summary = conn.execute(
                "SELECT affected_population_count, timestamp FROM fact_nba_trace WHERE nba_run_id=? AND step='RUN_SUMMARY'",
                (run_id,)
            ).fetchone()
            started = conn.execute(
                "SELECT MIN(timestamp) FROM fact_nba_trace WHERE nba_run_id=?", (run_id,)
            ).fetchone()
            sessions.append({
                "run_id": run_id,
                "opportunity": opp[0] if opp else "",
                "gaps_targeted": summary[0] if summary else 0,
                "completed_at": summary[1] if summary else (started[0] if started else ""),
            })
    return sessions


# ── GET /session/{run_id}/status ──────────────────────────────────────────────

@app.get("/session/{run_id}/status")
def get_session_status(run_id: str):
    with get_db() as conn:
        decisions = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_claude_decision WHERE nba_run_id = ?", (run_id,)).fetchall())
        campaigns = rows_as_dicts(conn.execute(
            "SELECT * FROM dim_nba_campaign WHERE nba_run_id = ?", (run_id,)).fetchall())
        outreach = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_outreach_plan WHERE nba_run_id = ?", (run_id,)).fetchall())
        trace = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_trace WHERE nba_run_id = ?", (run_id,)).fetchall())
    return {"decisions": decisions, "campaigns": campaigns, "outreach": outreach, "trace": trace}


# ── GET /session/latest ───────────────────────────────────────────────────────

@app.get("/session/latest")
def get_latest_session():
    with get_db() as conn:
        row = conn.execute(
            "SELECT nba_run_id FROM fact_nba_trace ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No sessions found")
        run_id = row["nba_run_id"]
        decisions = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_claude_decision WHERE nba_run_id = ?", (run_id,)).fetchall())
        campaigns = rows_as_dicts(conn.execute(
            "SELECT * FROM dim_nba_campaign WHERE nba_run_id = ?", (run_id,)).fetchall())
        outreach = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_outreach_plan WHERE nba_run_id = ?", (run_id,)).fetchall())
        trace = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_trace WHERE nba_run_id = ?", (run_id,)).fetchall())
    return {"run_id": run_id, "decisions": decisions, "campaigns": campaigns,
            "outreach": outreach, "trace": trace}


# ── DELETE /session/{run_id} ──────────────────────────────────────────────────

@app.delete("/session/{run_id}")
def delete_session(run_id: str):
    with get_db() as conn:
        counts = {}
        for table in ("fact_nba_claude_decision", "dim_nba_campaign",
                      "fact_nba_outreach_plan", "fact_nba_trace"):
            cur = conn.execute(f"DELETE FROM {table} WHERE nba_run_id = ?", (run_id,))
            counts[table] = cur.rowcount
    return {"status": "deleted", "run_id": run_id, "rows_deleted": counts}


# ── GET /messages/{run_id} ────────────────────────────────────────────────────

@app.get("/messages/{run_id}")
def get_messages(run_id: str):
    with get_db() as conn:
        rows = rows_as_dicts(conn.execute(
            """SELECT contact_id, member_gap_key, channel, planned_datetime,
                      incentive_offered, status, generated_message, sent_at
               FROM fact_nba_outreach_plan
               WHERE nba_run_id = ?
               ORDER BY created_timestamp""",
            (run_id,)
        ).fetchall())
    return rows


# ── POST /preview/messages/{run_id} ──────────────────────────────────────────

def _fetch_contact_context(conn, contact: dict) -> tuple[dict, dict, str, str, str]:
    """Return (member, gap, measure_name, measure_code, inc_label) for a contact row."""
    gap_row = conn.execute(
        "SELECT * FROM fact_member_gap WHERE member_gap_key = ?",
        (contact["member_gap_key"],)
    ).fetchone()
    gap = dict(gap_row) if gap_row else {}

    member_row = conn.execute(
        """SELECT m.*, c.email_allowed, c.sms_allowed, c.call_allowed,
                  c.preferred_channel, c.do_not_contact_flag
           FROM dim_member m
           JOIN dim_member_channel_pref c ON c.member_key = m.member_key
           WHERE m.member_key = ?""",
        (gap.get("member_key", ""),)
    ).fetchone()
    member = dict(member_row) if member_row else {}

    measure_row = conn.execute(
        "SELECT measure_name, measure_code FROM dim_measure WHERE measure_key = ?",
        (gap.get("measure_key", ""),)
    ).fetchone()
    if measure_row:
        measure_name, measure_code = measure_row["measure_name"], measure_row["measure_code"]
    else:
        measure_name = gap.get("measure_code", "your health screening")
        measure_code = gap.get("measure_code", "screening")

    incentive = contact.get("incentive_offered", "")
    inc_label = {
        "GIFTCARD_15": "$15 Gift Card", "GIFTCARD_25": "$25 Gift Card",
        "FIT_KIT_MAILER": "FIT Kit Mailer", "TRANSPORT_VOUCHER": "Transport Voucher",
        "NONE": "no additional incentive",
    }.get(incentive, incentive or "no additional incentive")

    return member, gap, measure_name, measure_code, inc_label


@app.post("/preview/messages/{run_id}")
def preview_messages(run_id: str):
    """Generate and store message previews for all contacts in a run without sending."""
    with get_db() as conn:
        contacts = rows_as_dicts(conn.execute(
            "SELECT * FROM fact_nba_outreach_plan WHERE nba_run_id = ? ORDER BY created_timestamp",
            (run_id,)
        ).fetchall())

    results = []
    for contact in contacts:
        if contact.get("generated_message"):
            # Already has a preview — return as-is
            results.append({"contact_id": contact["contact_id"],
                            "message_text": contact["generated_message"],
                            "message_source": "cached"})
            continue
        try:
            with get_db() as conn:
                member, gap, measure_name, measure_code, inc_label = _fetch_contact_context(conn, contact)
            message_text, message_source = _generate_message(
                contact["channel"], member, gap, measure_name, measure_code, inc_label
            )
            with get_db() as conn:
                conn.execute(
                    "UPDATE fact_nba_outreach_plan SET generated_message = ? WHERE contact_id = ?",
                    (message_text, contact["contact_id"])
                )
            results.append({"contact_id": contact["contact_id"],
                            "message_text": message_text,
                            "message_source": message_source})
        except Exception as e:
            results.append({"contact_id": contact["contact_id"],
                            "message_text": None,
                            "error": str(e)})
    return results


# ── Delivery helpers ──────────────────────────────────────────────────────────

def _is_call_channel(channel: str) -> bool:
    return "call" in channel.lower()


def _generate_message_template(channel: str, member: dict, gap: dict,
                               measure_name: str, measure_code: str,
                               incentive: str) -> str:
    """Fallback template-based message generation (no API key required)."""
    lang = member.get("language_preference", "EN")
    ch_lower = channel.lower()
    member_key = member.get("member_key", "Member")

    if ch_lower == "email":
        return (
            f"Subject: Important Health Reminder from Your Medicare Plan\n\n"
            f"Dear {member_key},\n\n"
            f"This is a friendly reminder that you have an outstanding {measure_name} "
            f"that needs to be completed this year.\n\n"
            f"As a valued member of your Medicare Advantage plan, your health is our priority. "
            f"Completing this important health check helps us ensure you receive the best "
            f"possible care.\n\n"
            f"As a thank you for completing this health activity, "
            f"you will receive {incentive}.\n\n"
            f"Please contact your primary care provider to schedule your appointment "
            f"at your earliest convenience.\n\n"
            f"If you have any questions please call us at 1-800-MEDICARE.\n\n"
            f"Thank you for taking care of your health.\n\n"
            f"Warm regards,\nCareIntel Health Outreach Team"
        )

    if _is_call_channel(channel):
        # Call script formatted for care manager to read aloud
        if "mandarin" in ch_lower or lang == "ZH":
            return (
                f"[电话脚本 — 普通话]\n\n"
                f"您好，请问是{member_key}吗？\n\n"
                f"我是来自您的医疗保险计划的健康顾问，今天致电是要提醒您今年还有一项重要的健康检查尚未完成：{measure_name}。\n\n"
                f"完成这项检查不仅对您的健康非常重要，您还将获得{incentive}作为感谢。\n\n"
                f"请问您是否方便与您的主治医生预约这项检查？我们可以协助您安排。\n\n"
                f"如有任何问题，请随时致电1-800-MEDICARE。感谢您配合，祝您身体健康！\n\n"
                f"[结束通话]"
            )
        elif "spanish" in ch_lower or lang == "ES":
            return (
                f"[Script de llamada — Español]\n\n"
                f"Buenos días, ¿puedo hablar con {member_key}?\n\n"
                f"Le llamo de parte de su plan Medicare. El motivo de la llamada es recordarle que "
                f"tiene pendiente completar su {measure_name} este año.\n\n"
                f"Como agradecimiento por completar este chequeo de salud, recibirá {incentive}.\n\n"
                f"¿Podría programar una cita con su médico de cabecera para realizar este examen? "
                f"Estamos aquí para ayudarle a coordinar la visita.\n\n"
                f"Si tiene alguna pregunta, llame al 1-800-MEDICARE. ¡Muchas gracias y que tenga un buen día!\n\n"
                f"[Fin de la llamada]"
            )
        else:
            return (
                f"[Call Script — English]\n\n"
                f"Hello, may I speak with {member_key}?\n\n"
                f"This is a health advisor calling from your Medicare Advantage plan. "
                f"I'm reaching out today because you have an outstanding {measure_name} "
                f"that needs to be completed this year.\n\n"
                f"As a thank you for completing this important health activity, "
                f"you will receive {incentive}.\n\n"
                f"Would you be able to schedule an appointment with your primary care provider "
                f"to complete this screening? We can also help coordinate the visit for you.\n\n"
                f"If you have any questions, please call us at 1-800-MEDICARE. "
                f"Thank you for your time, and have a wonderful day!\n\n"
                f"[End of call]"
            )

    # SMS — pick language variant
    if "mandarin" in ch_lower or lang == "ZH":
        return (
            f"您好 {member_key}, 提醒您今年完成{measure_code}检查, "
            f"可获得{incentive}。请致电1-800-MEDICARE预约。回复STOP退订。"
        )
    elif "spanish" in ch_lower or lang == "ES":
        return (
            f"Hola {member_key}, recordatorio: complete su {measure_code} este año "
            f"y reciba {incentive}. Llame al 1-800-MEDICARE para programar. "
            f"Responda STOP para cancelar."
        )
    else:
        return (
            f"Hi {member_key}, reminder: complete your {measure_code} this year "
            f"and receive {incentive}. Call 1-800-MEDICARE to schedule. "
            f"Reply STOP to opt out."
        )


def _generate_message_claude(channel: str, member: dict, gap: dict,
                             measure_name: str, incentive: str) -> str:
    """Claude-generated personalised message (requires ANTHROPIC_API_KEY)."""
    lang = member.get("language_preference", "EN")
    ch_lower = channel.lower()
    lang_note = ""
    if "mandarin" in ch_lower or lang == "ZH":
        lang_note = " Write the message in Mandarin Chinese."
    elif "spanish" in ch_lower or lang == "ES":
        lang_note = " Write the message in Spanish."

    user_prompt = (
        f"Write a {channel} message for a {member.get('age_band','senior')} year old "
        f"Medicare member who needs to complete {measure_name}. "
        f"The incentive being offered is {incentive}. "
        f"This gap has been open for {gap.get('days_open', 0)} days. "
        f"Tone should be warm and encouraging.{lang_note}"
    )

    client = anthropic.Anthropic(timeout=15.0)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=(
            "You are a healthcare outreach specialist writing member communications "
            "for a Medicare Advantage plan. Write warm, plain-language, respectful messages. "
            "Keep messages under 160 characters for SMS and under 200 words for email. "
            "Always include a clear call to action."
        ),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text.strip()


def _generate_message(channel: str, member: dict, gap: dict,
                      measure_name: str, measure_code: str,
                      incentive: str) -> tuple[str, str]:
    """Returns (message_text, message_source) using Claude if API key valid, else template."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    use_claude = bool(api_key and not api_key.startswith("paste_"))
    if use_claude:
        try:
            text = _generate_message_claude(channel, member, gap, measure_name, incentive)
            return text, "claude"
        except Exception as e:
            # Fall back to template on auth errors or any API failure
            logging.warning(f"Claude message generation failed ({e}), falling back to template")
    text = _generate_message_template(channel, member, gap, measure_name, measure_code, incentive)
    return text, "template"


def _resolve_channel(assigned_channel: str, member: dict) -> tuple[str, str | None]:
    """
    Validate and potentially override the assigned channel based on member preference.
    Returns (resolved_channel, override_note).
    resolved_channel == "SUPPRESSED" means skip this contact entirely.
    override_note is None when no change was made.
    """
    dnc = str(member.get("do_not_contact_flag", "false")).lower() == "true"
    if dnc:
        return "SUPPRESSED", "Member has Do Not Contact flag — skipping outreach"

    preferred = str(member.get("preferred_channel", "")).upper()
    email_ok  = str(member.get("email_allowed",  "false")).lower() == "true"
    sms_ok    = str(member.get("sms_allowed",    "false")).lower() == "true"
    call_ok   = str(member.get("call_allowed",   "false")).lower() == "true"
    member_key = member.get("member_key", "unknown")

    if preferred == "NONE":
        return "SUPPRESSED", f"Member {member_key} preferred_channel is NONE — skipping outreach"

    # Determine best channel following priority order
    if preferred == "EMAIL" and email_ok:
        resolved = "EMAIL"
    elif preferred == "SMS" and sms_ok:
        resolved = "SMS"
    elif preferred == "CALL" and call_ok:
        resolved = "CALL"
    elif email_ok:
        resolved = "EMAIL"
    elif sms_ok:
        resolved = "SMS"
    elif call_ok:
        resolved = "CALL"
    else:
        return "SUPPRESSED", f"No consented channel available for {member_key} — skipping outreach"

    # Check if an override is needed
    assigned_norm = assigned_channel.upper().split()[0]   # "SMS" from "Spanish SMS"
    resolved_norm = resolved.upper()
    if assigned_norm != resolved_norm:
        note = (f"Channel overridden for {member_key}: "
                f"assigned {assigned_channel} → sending via {resolved} "
                f"based on member preference")
        return resolved, note

    return assigned_channel, None


def send_email_gmail(to_email: str, subject: str, body: str) -> dict:
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = os.getenv('GMAIL_ADDRESS')
        msg['To'] = to_email

        html_body = f"""
        <html><body>
        <div style="font-family: Arial; max-width: 600px; margin: auto;">
            <div style="background: #F15A22; padding: 20px; color: white;">
                <h2>CareIntel Health Reminder</h2>
            </div>
            <div style="padding: 20px;">
                {body.replace(chr(10), '<br>')}
            </div>
            <div style="padding: 20px; color: gray; font-size: 12px;">
                This message is from your Medicare Advantage health plan.
                To unsubscribe reply STOP.
            </div>
        </div>
        </body></html>
        """

        msg.attach(MIMEText(body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as server:
            server.login(os.getenv('GMAIL_ADDRESS'), os.getenv('GMAIL_APP_PASSWORD'))
            server.send_message(msg)

        return {"success": True, "channel": "email", "delivered_to": to_email, "provider": "Gmail SMTP"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _send_whatsapp(to: str, body: str) -> dict:
    wa_from = os.getenv("TWILIO_WHATSAPP_NUMBER")
    wa_to   = "whatsapp:" + to
    client  = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(body=body, from_=wa_from, to=wa_to)
    return {"sid": msg.sid, "status": msg.status}




# ── POST /send/message/{contact_id} ──────────────────────────────────────────

@app.post("/send/message/{contact_id}")
def send_message(contact_id: str):
    # 1. Fetch contact row
    with get_db() as conn:
        contact_row = conn.execute(
            "SELECT * FROM fact_nba_outreach_plan WHERE contact_id = ?", (contact_id,)
        ).fetchone()
    if not contact_row:
        raise HTTPException(status_code=404, detail=f"Contact {contact_id} not found")
    contact = dict(contact_row)

    channel = contact.get("channel", "SMS")
    original_channel = channel

    # 2. Fetch gap + member + measure
    with get_db() as conn:
        member, gap, measure_name, measure_code, inc_label = _fetch_contact_context(conn, contact)

    # 2.5 DNC check + channel preference validation (FIX 1)
    channel, override_note = _resolve_channel(channel, member)
    if channel == "SUPPRESSED":
        with get_db() as conn:
            conn.execute(
                "UPDATE fact_nba_outreach_plan SET status='SUPPRESSED', error_reason=? WHERE contact_id=?",
                (override_note, contact_id),
            )
        return {
            "success": False, "channel": original_channel, "channel_used": "SUPPRESSED",
            "message_text": None, "message_source": None, "delivered_to": None,
            "status": "SUPPRESSED", "override_note": override_note, "error": override_note,
        }

    # 3. Generate message (Claude if API key present, else template fallback)
    try:
        message_text, message_source = _generate_message(
            channel, member, gap, measure_name, measure_code, inc_label
        )
    except Exception as e:
        err = f"Message generation failed: {e}"
        with get_db() as conn:
            conn.execute(
                "UPDATE fact_nba_outreach_plan SET status='FAILED', error_reason=? WHERE contact_id=?",
                (err, contact_id),
            )
        return {
            "success": False, "channel": channel, "channel_used": None,
            "message_text": None, "delivered_to": None,
            "status": "FAILED", "error": err, "override_note": override_note,
        }

    # 4. Deliver — EMAIL goes via Gmail SMTP, all other channels via WhatsApp sandbox
    delivered_to = None
    send_result = {}
    channel_used = None
    error_reason = None
    try:
        if channel.upper() == "EMAIL":
            test_email = os.getenv("TEST_EMAIL", "")
            subject = f"Health Reminder: {measure_name}"
            send_result = send_email_gmail(test_email, subject, message_text)
            if send_result.get("success"):
                channel_used = "EMAIL"
                delivered_to = test_email
                success = True
                new_status = "SENT"
            else:
                raise Exception(send_result.get("error", "Gmail send failed"))
        else:
            channel_used = "WhatsApp"
            delivered_to = "whatsapp:" + TEST_SMS
            send_result = _send_whatsapp(TEST_SMS, message_text)
            success = True
            new_status = "SENT"
    except Exception as e:
        # Extract the most useful part of Twilio errors
        raw = str(e)
        if hasattr(e, 'msg'):
            raw = e.msg
        elif hasattr(e, 'message') and e.message:
            raw = e.message
        # Twilio errors often contain "Unable to create record: ..." — trim to that
        if "Unable to create record" in raw:
            raw = raw.split("Unable to create record")[-1].strip(": ")
        error_reason = raw[:300]
        success = False
        new_status = "FAILED"
        channel_used = channel_used or channel
        send_result = {"error": error_reason}

    # 5. Persist result + create WhatsApp conversation row for SMS/WhatsApp channels
    sent_at = datetime.now().isoformat(timespec="seconds") if success else None
    with get_db() as conn:
        conn.execute(
            """UPDATE fact_nba_outreach_plan
               SET status = ?, generated_message = ?, sent_at = ?, error_reason = ?
               WHERE contact_id = ?""",
            (new_status, message_text, sent_at, error_reason, contact_id),
        )
        # Track sent messages so the evaluation tab shows status for all channels
        if success and channel_used in ("WhatsApp", "EMAIL"):
            mgk = contact.get("member_gap_key", "")
            run_id = contact.get("nba_run_id", "")
            # For EMAIL use the test email address as the identifier; for WhatsApp use the phone
            contact_identifier = (os.getenv("TEST_EMAIL", "") if channel_used == "EMAIL"
                                  else (TEST_SMS or ""))
            init_state = "EMAIL_SENT" if channel_used == "EMAIL" else "OUTREACH_SENT"
            conn.execute(
                """INSERT OR IGNORE INTO whatsapp_conversations
                   (conversation_id, member_gap_key, contact_id, nba_run_id,
                    member_phone, member_key, measure_name,
                    conversation_state, created_timestamp, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f"CONV_{contact_id}", mgk, contact_id, run_id,
                 contact_identifier, member.get("member_key",""), measure_name,
                 init_state, sent_at, sent_at)
            )

    return {
        "success": success,
        "channel": original_channel,
        "channel_used": channel_used,
        "override_note": override_note,
        "message_text": message_text,
        "message_source": message_source,
        "delivered_to": delivered_to,
        "status": new_status,
        "error": error_reason,
        "send_result": send_result,
    }


# ── GET /test/email ──────────────────────────────────────────────────────────

@app.get("/test/email")
def test_email():
    """Send a test email via Gmail SMTP to the TEST_EMAIL address."""
    test_email = os.getenv("TEST_EMAIL", "")
    if not test_email:
        raise HTTPException(status_code=500, detail="TEST_EMAIL not configured in .env")
    result = send_email_gmail(
        to_email=test_email,
        subject="CareIntel — Email Delivery Test",
        body="This is a test message from CareIntel NBA.\n\nIf you received this, Gmail SMTP is working correctly.",
    )
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"Gmail SMTP failed: {result.get('error')}")
    return result


# ── POST /send/all/{run_id} ───────────────────────────────────────────────────

@app.post("/send/all/{run_id}")
def send_all(run_id: str):
    with get_db() as conn:
        contacts = rows_as_dicts(conn.execute(
            """SELECT contact_id FROM fact_nba_outreach_plan
               WHERE nba_run_id = ? AND status = 'PLANNED'""",
            (run_id,)
        ).fetchall())

    sent, failed, suppressed, overridden = 0, 0, 0, 0
    results = []
    for c in contacts:
        r = send_message(c["contact_id"])
        results.append({"contact_id": c["contact_id"], **r})
        status = r.get("status")
        if status == "SUPPRESSED":
            suppressed += 1
        elif not r.get("success"):
            failed += 1
        else:
            sent += 1
            if r.get("override_note"):
                overridden += 1

    preferred_match = sent - overridden

    # FIX 3 — Channel optimization trace row
    trace_row = {
        "nba_run_id": run_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "agent": "Outreach",
        "step": "CHANNEL_OPTIMIZATION",
        "input_summary": f"{len(contacts)} contacts processed",
        "output_summary": (
            f"Channel optimization: {preferred_match} preferred match, "
            f"{overridden} overridden, {suppressed} suppressed"
        ),
        "affected_population_count": sent,
    }
    cols = list(trace_row.keys())
    with get_db() as conn:
        conn.execute(
            f"INSERT INTO fact_nba_trace ({', '.join(cols)}) VALUES ({', '.join('?'*len(cols))})",
            list(trace_row.values()),
        )

    # Safeguard: backfill any WhatsApp conversation rows that were dropped
    # under concurrent SQLite write pressure during rapid send_all loops
    _backfill_conversations(run_id)

    return {
        "run_id": run_id,
        "total": len(contacts),
        "sent": sent,
        "failed": failed,
        "suppressed": suppressed,
        "overridden": overridden,
        "preferred_match": preferred_match,
        "results": results,
    }


def _backfill_conversations(run_id: str):
    """Create missing whatsapp_conversations rows for all SENT SMS contacts in a run."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    phone = os.getenv("TEST_SMS", "")
    with get_db() as conn:
        missing = conn.execute(
            """SELECT o.contact_id, o.member_gap_key, o.nba_run_id, o.sent_at,
                      g.member_key, g.measure_key as measure_name
               FROM fact_nba_outreach_plan o
               LEFT JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
               LEFT JOIN whatsapp_conversations wc ON wc.contact_id = o.contact_id
               WHERE o.nba_run_id = ? AND o.status = 'SENT'
                 AND wc.conversation_id IS NULL""",
            (run_id,)
        ).fetchall()
        for r in missing:
            r = dict(r)
            cid = r["contact_id"]
            conn.execute(
                """INSERT OR IGNORE INTO whatsapp_conversations
                   (conversation_id, member_gap_key, contact_id, nba_run_id,
                    member_phone, member_key, measure_name,
                    conversation_state, created_timestamp, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f"CONV_{cid}", r["member_gap_key"], cid, r["nba_run_id"],
                 phone, r["member_key"] or "", r["measure_name"] or "",
                 "OUTREACH_SENT", r["sent_at"] or now_iso, r["sent_at"] or now_iso)
            )


@app.post("/conversations/backfill/{run_id}")
def backfill_conversations(run_id: str):
    """Manually trigger conversation backfill for a run (e.g. after a partial send)."""
    _backfill_conversations(run_id)
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM whatsapp_conversations WHERE nba_run_id=?", (run_id,)
        ).fetchone()[0]
    return {"run_id": run_id, "total_conversations": total}


# ── Evaluation Agent helpers ──────────────────────────────────────────────────

def _eval_corrective_action(member: dict, gap: dict, channel_pref: dict, days_since: int) -> tuple[str, str]:
    if gap.get("gap_status") == "Closed":
        return "NO_ACTION", "Gap already closed following outreach"
    risk = float(gap.get("clinical_risk_score") or 0.5)
    lit  = (member.get("digital_literacy_segment") or "Medium")
    soc  = (member.get("socioeconomic_segment") or "Mid")
    if risk >= 0.75:
        return "CARE_MANAGER_CALL", f"High clinical risk ({risk:.2f}) — escalate to care manager"
    if days_since >= 21 and (channel_pref.get("call_allowed") or "").lower() == "true":
        return "SWITCH_CHANNEL", f"No response after {days_since} days — try call channel"
    if soc == "Low":
        return "ESCALATE_INCENTIVE", "Low SES member — increase incentive to improve engagement"
    if lit == "Low":
        return "CARE_MANAGER_CALL", "Low digital literacy — human outreach more effective"
    if days_since <= 7:
        return "EXTEND_CAMPAIGN", "Within initial 7-day window — allow more time before escalation"
    return "NO_ACTION", "Member profile suggests no additional action warranted"


def _run_evaluation(run_id: str) -> dict:
    now_iso  = datetime.now().isoformat(timespec="seconds")
    today_dt = date.today()
    today_s  = str(today_dt)

    with get_db() as conn:
        campaign = conn.execute(
            "SELECT campaign_id FROM dim_nba_campaign WHERE nba_run_id = ?", (run_id,)
        ).fetchone()
        camp_id = campaign["campaign_id"] if campaign else f"CMP_{run_id[4:]}_01"

        contacts = conn.execute("""
            SELECT o.contact_id, o.member_gap_key, o.channel, o.sent_at,
                   g.member_key, g.gap_status, g.clinical_risk_score,
                   m.digital_literacy_segment, m.socioeconomic_segment,
                   p.email_allowed, p.sms_allowed, p.call_allowed, p.preferred_channel
            FROM fact_nba_outreach_plan o
            LEFT JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
            LEFT JOIN dim_member m ON m.member_key = g.member_key
            LEFT JOIN dim_member_channel_pref p ON p.member_key = g.member_key
            WHERE o.nba_run_id = ?
              AND o.status IN ('SENT','SCHEDULED','COMPLETED')
        """, (run_id,)).fetchall()

        if not contacts:
            raise HTTPException(status_code=404, detail=f"No sent contacts for run {run_id}")

        # Determine evaluation window from first sent date
        first_sent_str = min((c["sent_at"] or today_s) for c in contacts)
        try:
            sent_date = date.fromisoformat(first_sent_str[:10])
        except Exception:
            sent_date = today_dt - timedelta(days=14)

        days_elapsed = (today_dt - sent_date).days
        window = 7 if days_elapsed < 14 else (14 if days_elapsed < 30 else 30)

        # Measure-specific HEDIS benchmarks (Day 7 base rates)
        _HEDIS_BASE_7 = {
            "MAD": 0.45, "AFV": 0.35, "CDC": 0.25, "EED": 0.15,
            "BCS": 0.12, "COL": 0.10, "SPC": 0.40,
        }
        _HEDIS_30 = {
            "MAD": 0.75, "AFV": 0.65, "CDC": 0.50, "EED": 0.35,
            "BCS": 0.30, "COL": 0.25, "SPC": 0.70,
        }
        _DEFAULT_BASE = {"7": 0.20, "14": 0.50, "30": 0.80}

        # Resolve HEDIS measure_code (BCS/COL/EED/etc.) from majority of contacts in this run
        measure_codes = conn.execute(
            """SELECT d.measure_code, COUNT(*) as cnt
               FROM fact_nba_outreach_plan o
               LEFT JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
               LEFT JOIN dim_measure d ON d.measure_key = g.measure_key
               WHERE o.nba_run_id = ?
               GROUP BY d.measure_code ORDER BY cnt DESC LIMIT 1""",
            (run_id,)
        ).fetchone()
        measure_code = (measure_codes["measure_code"] if measure_codes else None) or "UNKNOWN"

        # Look up the benchmark; fall back to flat defaults
        if window == 7:
            exp_rate = _HEDIS_BASE_7.get(measure_code, 0.20)
        elif window == 14:
            exp_rate = round(min(_HEDIS_BASE_7.get(measure_code, 0.20) * 1.8, 0.90), 3)
        else:
            exp_rate = _HEDIS_30.get(measure_code, 0.80)

        total = len(contacts)
        closed_actual = sum(1 for c in contacts if (c["gap_status"] or "") == "Closed")
        actual_rate = round(closed_actual / total, 3) if total else 0.0
        exp_closed = max(1, round(total * exp_rate))

        # Threshold: ±15% of expected (relative, not absolute)
        threshold = exp_rate * 0.15
        diff = actual_rate - exp_rate
        if diff > threshold:
            perf_status = "Overperforming"
        elif diff >= -threshold:
            perf_status = "On Track"
        else:
            perf_status = "Underperforming"

        # ── Correct Stars impact formula ──────────────────────────────────────
        # stars_per_gap = (1 / estimated_denominator) × star_weight × 0.5
        # denominator = plan_total_members × eligibility_rate_for_measure
        _ELIGIBILITY_RATE = {
            "BCS": 0.30, "COL": 0.45, "EED": 0.15, "CDC": 0.35,
            "MAD": 0.15, "AFV": 0.80, "SPC": 0.20,
        }
        _PLAN_MEMBERS_BY_SEGMENT = {"MAPD": 500, "DSNP": 300, "MA-only": 400}

        plan_row = conn.execute(
            """SELECT p.segment FROM dim_nba_campaign c
               LEFT JOIN dim_plan_contract p ON p.plan_key = c.plan_key
               WHERE c.nba_run_id = ? LIMIT 1""", (run_id,)
        ).fetchone()
        plan_segment = (plan_row["segment"] if plan_row else None) or "MAPD"
        plan_total   = _PLAN_MEMBERS_BY_SEGMENT.get(plan_segment, 450)

        elig_rate  = _ELIGIBILITY_RATE.get(measure_code, 0.30)
        denominator = max(plan_total * elig_rate, 1)

        star_weight_row = conn.execute(
            "SELECT star_weight FROM dim_measure WHERE measure_code = ?", (measure_code,)
        ).fetchone()
        star_weight = float(star_weight_row["star_weight"] if star_weight_row else 1.0)

        stars_per_gap   = (1 / denominator) * star_weight * 0.5
        stars_actual    = round(min(closed_actual * stars_per_gap, 0.50), 4)
        stars_proj      = round(min(exp_closed    * stars_per_gap, 0.50), 4)

        if perf_status == "On Track":
            advice = "Campaign is performing in line with expectations. Continue current strategy."
        elif perf_status == "Underperforming":
            advice = "Consider escalating incentives or switching channels for non-responders."
        else:
            advice = "Exceeding targets. Consider closing campaign early to reallocate resources."

        summary = (
            f"Campaign {camp_id} evaluated at day {window}. "
            f"{closed_actual} of {total} members contacted have closed their gap "
            f"({round(actual_rate*100)}% actual vs {round(exp_rate*100)}% expected). "
            f"Status: {perf_status}. "
            f"Estimated Stars impact: +{stars_actual:.4f} pts actual, +{stars_proj:.4f} pts projected "
            f"(based on {denominator:.0f} eligible members, star weight {star_weight}). "
            + advice
        )

        eval_id = f"EVAL_{run_id[4:]}_{window}D_{today_s}"
        conn.execute(
            """INSERT OR REPLACE INTO campaign_evaluations
               (evaluation_id, nba_run_id, campaign_id, evaluation_date, evaluation_window,
                measure_code,
                total_members_contacted, gaps_closed_actual, gaps_closed_expected,
                actual_closure_rate, expected_closure_rate, performance_status,
                stars_impact_actual, stars_impact_projected, executive_summary, created_timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eval_id, run_id, camp_id, today_s, window,
             measure_code,
             total, closed_actual, exp_closed, actual_rate, exp_rate, perf_status,
             stars_actual, stars_proj, summary, now_iso)
        )

        # Member-level evaluations
        member_evals = []
        for c in contacts:
            days_since = (today_dt - sent_date).days
            action, reason = _eval_corrective_action(
                dict(c), dict(c), dict(c), days_since
            )
            responded = 1 if (c["gap_status"] or "") == "Closed" else 0
            follow_up = str(today_dt + timedelta(days=7)) if action not in ("NO_ACTION", "CLOSE_CAMPAIGN") else ""
            mem_eval_id = f"MEVAL_{run_id[4:]}_{c['contact_id']}_{today_s}"
            conn.execute(
                """INSERT OR REPLACE INTO member_evaluations
                   (member_eval_id, evaluation_id, nba_run_id, contact_id, member_gap_key,
                    member_key, outreach_sent_date, gap_status_at_evaluation, days_since_outreach,
                    responded, recommended_action, action_reason, follow_up_scheduled, created_timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mem_eval_id, eval_id, run_id, c["contact_id"], c["member_gap_key"],
                 c["member_key"], c["sent_at"] or str(sent_date),
                 c["gap_status"] or "Open", days_since, responded,
                 action, reason, follow_up, now_iso)
            )
            member_evals.append({
                "contact_id": c["contact_id"],
                "member_key": c["member_key"],
                "gap_status": c["gap_status"],
                "days_since_outreach": days_since,
                "recommended_action": action,
                "action_reason": reason,
                "follow_up_scheduled": follow_up,
            })

        # Confirmed-closed gaps → update outreach to COMPLETED + formalize gap closure
        closed_contacts = [c for c in contacts if (c["gap_status"] or "") == "Closed"]
        for c in closed_contacts:
            conn.execute(
                "UPDATE fact_nba_outreach_plan SET status='COMPLETED' WHERE contact_id=?", (c["contact_id"],)
            )
            if c["member_gap_key"]:
                conn.execute(
                    "UPDATE fact_member_gap SET gap_status='Closed' WHERE member_gap_key=?",
                    (c["member_gap_key"],)
                )
                conn.execute(
                    """INSERT INTO fact_nba_trace
                       (nba_run_id, timestamp, agent, step,
                        input_summary, output_summary, affected_population_count)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run_id, now_iso, "EvaluationAgent", "GAP_CLOSED",
                     f"Gap {c['member_gap_key']} confirmed closed via evaluation",
                     f"Member {c['member_key']} gap status set to Closed; outreach COMPLETED", 1)
                )

        # Auto-schedule next window if not already scheduled
        next_window = {7: 14, 14: 30}.get(window)
        if next_window:
            next_date = sent_date + timedelta(days=next_window)
            sched_id = f"SCHED_{run_id[4:]}_{next_window}D"
            conn.execute(
                """INSERT OR IGNORE INTO evaluation_schedule
                   (schedule_id, nba_run_id, campaign_id, scheduled_date, evaluation_window, status, created_timestamp)
                   VALUES (?,?,?,?,?,?,?)""",
                (sched_id, run_id, camp_id, str(next_date), next_window, "PENDING", now_iso)
            )

        # Update schedule row to COMPLETED
        conn.execute(
            """UPDATE evaluation_schedule SET status='COMPLETED'
               WHERE nba_run_id=? AND evaluation_window=?""",
            (run_id, window)
        )

        # Audit trace
        conn.execute(
            """INSERT INTO fact_nba_trace
               (nba_run_id, timestamp, agent, step,
                input_summary, output_summary, affected_population_count)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, now_iso, "EvaluationAgent", f"EVALUATE_DAY_{window}",
             f"Evaluated {total} sent contacts at day {window}",
             f"{perf_status}: {closed_actual}/{total} closed ({round(actual_rate*100)}%)",
             total)
        )

    return {
        "evaluation_id": eval_id,
        "run_id": run_id,
        "campaign_id": camp_id,
        "evaluation_date": today_s,
        "evaluation_window": window,
        "measure_code": measure_code,
        "performance_status": perf_status,
        "total_members_contacted": total,
        "gaps_closed_actual": closed_actual,
        "gaps_closed_expected": exp_closed,
        "actual_closure_rate": actual_rate,
        "expected_closure_rate": exp_rate,
        "stars_impact_actual": stars_actual,
        "stars_impact_projected": stars_proj,
        "executive_summary": summary,
        "member_evaluations": member_evals,
    }


# ── Evaluation endpoints (static routes BEFORE parameterised ones) ────────────

@app.get("/evaluate/all")
def get_all_evaluations():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ce.* FROM campaign_evaluations ce
               INNER JOIN (
                   SELECT nba_run_id, MAX(created_timestamp) AS latest
                   FROM campaign_evaluations GROUP BY nba_run_id
               ) best ON ce.nba_run_id = best.nba_run_id
                      AND ce.created_timestamp = best.latest
               ORDER BY ce.evaluation_date DESC, ce.nba_run_id"""
        ).fetchall()
        total_campaigns = len(rows)
        on_track = sum(1 for r in rows if r["performance_status"] == "On Track")
        under    = sum(1 for r in rows if r["performance_status"] == "Underperforming")
        over     = sum(1 for r in rows if r["performance_status"] == "Overperforming")
        total_members = sum(r["total_members_contacted"] or 0 for r in rows)
        total_closed  = sum(r["gaps_closed_actual"] or 0 for r in rows)
        stars_actual  = round(sum(r["stars_impact_actual"] or 0 for r in rows), 2)
        stars_proj    = round(sum(r["stars_impact_projected"] or 0 for r in rows), 2)
    return {
        "portfolio_summary": {
            "total_campaigns_evaluated": total_campaigns,
            "on_track": on_track,
            "underperforming": under,
            "overperforming": over,
            "total_members_contacted": total_members,
            "total_gaps_closed": total_closed,
            "stars_impact_actual": stars_actual,
            "stars_impact_projected": stars_proj,
        },
        "campaigns": [dict(r) for r in rows],
    }


@app.post("/evaluate/schedule/{run_id}")
def schedule_evaluation(run_id: str):
    """Create evaluation schedule rows (Day 7 / 14 / 30) for a completed run."""
    from datetime import date, timedelta
    today = date.today()
    windows = [7, 14, 30]
    rows_written = 0
    with get_db() as conn:
        existing = {r[0] for r in conn.execute(
            "SELECT evaluation_window FROM evaluation_schedule WHERE nba_run_id=?", (run_id,)
        ).fetchall()}
        for w in windows:
            if w in existing:
                continue
            sched_date = str(today + timedelta(days=w))
            conn.execute(
                """INSERT INTO evaluation_schedule (nba_run_id, evaluation_window, scheduled_date, status)
                   VALUES (?,?,?,'PENDING')""",
                (run_id, w, sched_date)
            )
            rows_written += 1
    schedule = [{"evaluation_window": w, "scheduled_date": str(today + timedelta(days=w))} for w in windows]
    return {"run_id": run_id, "schedule": schedule, "rows_written": rows_written}


@app.get("/evaluate/schedule/due")
def get_due_evaluations():
    today_s = str(date.today())
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM evaluation_schedule
               WHERE scheduled_date <= ? AND status = 'PENDING'
               ORDER BY scheduled_date""",
            (today_s,)
        ).fetchall()
    return {"due_count": len(rows), "due_evaluations": [dict(r) for r in rows]}


@app.post("/evaluate/run-scheduled")
def run_scheduled_evaluations():
    today_s = str(date.today())
    with get_db() as conn:
        due = conn.execute(
            """SELECT DISTINCT nba_run_id FROM evaluation_schedule
               WHERE scheduled_date <= ? AND status = 'PENDING'""",
            (today_s,)
        ).fetchall()

    results = []
    for row in due:
        run_id = row["nba_run_id"]
        try:
            result = _run_evaluation(run_id)
            results.append({"run_id": run_id, "status": "evaluated",
                            "performance_status": result["performance_status"]})
        except Exception as e:
            results.append({"run_id": run_id, "status": "error", "error": str(e)})

    return {"triggered": len(due), "results": results}


@app.post("/evaluate/{run_id}")
def evaluate_run(run_id: str):
    return _run_evaluation(run_id)


@app.get("/evaluate/{run_id}/latest")
def get_latest_evaluation(run_id: str):
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM campaign_evaluations
               WHERE nba_run_id = ?
               ORDER BY evaluation_date DESC, evaluation_window DESC LIMIT 1""",
            (run_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"No evaluation found for {run_id}")
        eval_dict = dict(row)

        members = conn.execute(
            """SELECT me.*,
                      COALESCE(dm.display_name, me.member_key) AS display_name,
                      wc.conversation_state, wc.appointment_date
               FROM member_evaluations me
               LEFT JOIN dim_member dm ON dm.member_key = me.member_key
               LEFT JOIN whatsapp_conversations wc ON wc.contact_id = me.contact_id
               WHERE me.evaluation_id = ?
               ORDER BY me.days_since_outreach DESC""",
            (eval_dict["evaluation_id"],)
        ).fetchall()
        eval_dict["member_evaluations"] = [dict(m) for m in members]
    return eval_dict



# ── Outcome recording endpoint ────────────────────────────────────────────────

OUTCOME_GAP_STATUS = {
    "Gap Closed":         "Closed",
    "Already Completed":  "Closed",
    "No Response":        "Open",
    "Wrong Number":       "Open",
    "Opted Out":          "Suppressed",
}

OUTCOME_OUTREACH_STATUS = {
    "Gap Closed":         "COMPLETED",
    "Already Completed":  "COMPLETED",
    "No Response":        "NO_RESPONSE",
    "Wrong Number":       "UNREACHABLE",
    "Opted Out":          "OPTED_OUT",
}


@app.post("/outcome/{contact_id}")
def record_outcome(contact_id: str, body: dict):
    outcome = (body.get("outcome") or "").strip()
    if outcome not in OUTCOME_GAP_STATUS:
        raise HTTPException(status_code=400, detail=f"Unknown outcome '{outcome}'. Must be one of: {list(OUTCOME_GAP_STATUS)}")

    now_iso  = datetime.now().isoformat(timespec="seconds")
    today_s  = str(date.today())
    new_gap_status      = OUTCOME_GAP_STATUS[outcome]
    new_outreach_status = OUTCOME_OUTREACH_STATUS[outcome]

    with get_db() as conn:
        row = conn.execute(
            "SELECT nba_run_id, member_gap_key FROM fact_nba_outreach_plan WHERE contact_id=?", (contact_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Contact {contact_id} not found")
        run_id = row["nba_run_id"]
        mgk    = row["member_gap_key"]

        # 1. Update outreach plan status
        conn.execute(
            "UPDATE fact_nba_outreach_plan SET status=?, sent_at=COALESCE(sent_at,?) WHERE contact_id=?",
            (new_outreach_status, now_iso, contact_id)
        )

        # 2. Update gap status
        if mgk:
            conn.execute(
                "UPDATE fact_member_gap SET gap_status=? WHERE member_gap_key=?",
                (new_gap_status, mgk)
            )

        # 3. Get member_key for trace
        gap_row = conn.execute(
            "SELECT member_key FROM fact_member_gap WHERE member_gap_key=?", (mgk,)
        ).fetchone() if mgk else None
        member_key = gap_row["member_key"] if gap_row else "?"

        # 4. Write trace
        conn.execute(
            """INSERT INTO fact_nba_trace
               (nba_run_id, timestamp, agent, step,
                input_summary, output_summary, affected_population_count)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, now_iso, "EvaluationAgent", "OUTCOME_RECORDED",
             f"Outcome recorded for {member_key}: {outcome} on {today_s}",
             f"Gap {mgk} status updated to {new_gap_status}; outreach → {new_outreach_status}", 1)
        )

    # 5. Re-evaluate the campaign and return updated results
    return _run_evaluation(run_id)


# ── Background auto-evaluation scheduler (STEP 4) ────────────────────────────

logging.basicConfig(level=logging.INFO)
_eval_logger = logging.getLogger("eval_scheduler")


def _auto_eval_loop():
    while True:
        _eval_logger.info("Auto-evaluation scheduler running — checking for due evaluations")
        try:
            today_s = str(date.today())
            with get_db() as conn:
                due = conn.execute(
                    """SELECT DISTINCT nba_run_id FROM evaluation_schedule
                       WHERE scheduled_date <= ? AND status = 'PENDING'""",
                    (today_s,)
                ).fetchall()
            for row in due:
                try:
                    _run_evaluation(row["nba_run_id"])
                    _eval_logger.info(f"Auto-evaluated run {row['nba_run_id']}")
                except Exception as e:
                    _eval_logger.warning(f"Auto-eval failed for {row['nba_run_id']}: {e}")
            # Also send WhatsApp follow-ups
            _send_conversation_followups()
        except Exception as e:
            _eval_logger.error(f"Scheduler error: {e}")
        threading.Event().wait(86400)


_scheduler_thread = threading.Thread(target=_auto_eval_loop, daemon=True, name="eval-scheduler")
_scheduler_thread.start()


# ── WhatsApp conversation state machine ───────────────────────────────────────

_YES_WORDS  = {"yes","yeah","sure","ok","okay","will do","i will","going","scheduled","yep","yup","sounds good","definitely","absolutely","of course"}
_NO_WORDS   = {"no","cant","cannot","busy","later","not now","maybe later","nope","nah","unfortunately","not yet"}
_DONE_WORDS = {"yes","done","completed","went","finished","went","did it","got it done","i did","all done","yes i did"}
_NOT_YET    = {"no","not yet","havent","did not","didn't","haven't","nope","not done"}
_STOP_WORDS = {"stop","unsubscribe","optout","opt out","opt-out","cancel","end","quit"}

_MONTH_MAP = {
    "january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,
    "april":4,"apr":4,"may":5,"june":6,"jun":6,"july":7,"jul":7,
    "august":8,"aug":8,"september":9,"sep":9,"sept":9,"october":10,"oct":10,
    "november":11,"nov":11,"december":12,"dec":12,
}

def _parse_date_from_text(text: str) -> str | None:
    t = text.lower().strip()
    today_dt = date.today()

    if "tomorrow" in t:
        return str(today_dt + timedelta(days=1))
    if "next week" in t:
        return str(today_dt + timedelta(days=7))
    if "this week" in t:
        return str(today_dt + timedelta(days=3))
    # next Monday/Tuesday etc.
    days_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    for day_name, day_num in days_map.items():
        if day_name in t:
            days_ahead = (day_num - today_dt.weekday() + 7) % 7 or 7
            return str(today_dt + timedelta(days=days_ahead))

    # MM/DD or DD/MM
    m = re.search(r'\b(\d{1,2})[/\-](\d{1,2})\b', t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        try:
            if a <= 12:
                return str(date(today_dt.year, a, b))
            return str(date(today_dt.year, b, a))
        except ValueError:
            pass

    # "July 15" or "15th July" etc.
    m = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)', t)
    if m:
        day_n, mon_s = int(m.group(1)), m.group(2)
        mon = _MONTH_MAP.get(mon_s)
        if mon:
            try:
                return str(date(today_dt.year, mon, day_n))
            except ValueError:
                pass
    m = re.search(r'([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?', t)
    if m:
        mon_s, day_n = m.group(1), int(m.group(2))
        mon = _MONTH_MAP.get(mon_s)
        if mon:
            try:
                return str(date(today_dt.year, mon, day_n))
            except ValueError:
                pass
    return None


def _wa_reply(to_phone: str, body: str):
    wa_from = os.getenv("TWILIO_WHATSAPP_NUMBER")
    wa_to = "whatsapp:" + to_phone
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(from_=wa_from, to=wa_to, body=body)
    except Exception as e:
        _eval_logger.warning(f"WA reply failed to {to_phone}: {e}")


def _twiml(msg: str) -> Response:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Message>{msg}</Message></Response>"""
    return Response(content=xml, media_type="application/xml")


def _contains_any(text: str, words: set) -> bool:
    t = text.lower().strip()
    return any(w == t or f" {w} " in f" {t} " or t.startswith(w+" ") or t.endswith(" "+w) for w in words)


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    from_raw = form.get("From", "")
    body_raw  = (form.get("Body", "") or "").strip()
    # Twilio sends From as "whatsapp:+14155..." — normalize regardless of URL encoding
    phone = from_raw.replace("whatsapp:","").strip().replace(" ","")
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    now_iso = datetime.now().isoformat(timespec="seconds")
    today_s = str(date.today())

    with get_db() as conn:
        conv = conn.execute(
            """SELECT * FROM whatsapp_conversations
               WHERE member_phone = ?
               ORDER BY last_updated DESC LIMIT 1""",
            (phone,)
        ).fetchone()

    if not conv:
        return _twiml("Hello! We don't have an active outreach for this number. Please contact your care team.")

    conv = dict(conv)
    state = conv["conversation_state"]
    conv_id = conv["conversation_id"]
    run_id  = conv["nba_run_id"]
    mgk     = conv["member_gap_key"]
    cid     = conv["contact_id"]
    measure = conv["measure_name"] or "health screening"

    reply_msg = ""

    # ── OUTREACH_SENT ──────────────────────────────────────────────────────────
    if state == "OUTREACH_SENT":
        if _contains_any(body_raw, _STOP_WORDS):
            with get_db() as conn:
                member_key = conv.get("member_key","")
                if member_key:
                    conn.execute(
                        "UPDATE dim_member_channel_pref SET do_not_contact_flag='true' WHERE member_key=?",
                        (member_key,)
                    )
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='DECLINED',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
                conn.execute(
                    """INSERT INTO fact_nba_trace (nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run_id, now_iso, "WhatsAppAgent", "MEMBER_OPT_OUT",
                     f"Member {conv['member_key']} sent STOP", "DNC flag set; conversation DECLINED", 1)
                )
            return _twiml("You have been unsubscribed from health reminders. Reply START to resubscribe anytime.")

        elif _contains_any(body_raw, _YES_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='AWAITING_DATE',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "Great! What date are you planning to go? Please reply with the date (e.g. July 15)"

        elif _contains_any(body_raw, _NO_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='DECLINED',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "No problem! We will check back with you in 2 weeks. Your health matters to us."

        else:
            reply_msg = f"Hi! Are you planning to schedule your {measure} appointment? Reply YES to let us know or NO if you need more time."

    # ── AWAITING_DATE ──────────────────────────────────────────────────────────
    elif state == "AWAITING_DATE":
        parsed = _parse_date_from_text(body_raw)
        if parsed:
            with get_db() as conn:
                conn.execute(
                    """UPDATE whatsapp_conversations
                       SET conversation_state='DATE_CONFIRMED', appointment_date=?, last_inbound_msg=?, last_updated=?
                       WHERE conversation_id=?""",
                    (parsed, body_raw, now_iso, conv_id)
                )
            try:
                friendly = date.fromisoformat(parsed).strftime("%B %d").lstrip("0").replace(" 0"," ")
            except Exception:
                friendly = parsed
            reply_msg = f"Perfect! We have noted your appointment for {friendly}. We will check in with you a few days after to see how it went. Good luck!"
        else:
            reply_msg = "Thanks! Could you share the date you are planning to go? For example: July 15"
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )

    # ── FOLLOW_UP_SENT ─────────────────────────────────────────────────────────
    elif state == "FOLLOW_UP_SENT":
        if _contains_any(body_raw, _DONE_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='COMPLETED',gap_closed=1,last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
                if mgk:
                    conn.execute("UPDATE fact_member_gap SET gap_status='Closed' WHERE member_gap_key=?", (mgk,))
                if cid:
                    conn.execute("UPDATE fact_nba_outreach_plan SET status='COMPLETED' WHERE contact_id=?", (cid,))
                conn.execute(
                    """INSERT INTO fact_nba_trace (nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run_id, now_iso, "WhatsAppAgent", "GAP_CLOSED",
                     f"Gap {mgk} closed via WhatsApp conversation",
                     "Member confirmed completion — gap_status set to Closed", 1)
                )
            reply_msg = "That is wonderful news! Thank you for taking care of your health. Your care team has been notified."
            # Trigger re-evaluation asynchronously (best-effort)
            try: _run_evaluation(run_id)
            except Exception: pass

        elif _contains_any(body_raw, _NOT_YET):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='DECLINED',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "No worries! Would you like to schedule another appointment? Reply YES to get a new reminder or STOP to opt out."

        else:
            reply_msg = f"Thanks for your reply! Did you manage to complete your {measure} appointment? Reply YES if done or NO if not yet."
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )

    else:
        reply_msg = f"Thanks for your message! Your care team has been notified. State: {state}"
        with get_db() as conn:
            conn.execute(
                "UPDATE whatsapp_conversations SET last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                (body_raw, now_iso, conv_id)
            )

    return _twiml(reply_msg)


# ── Conversation endpoints ─────────────────────────────────────────────────────

@app.get("/conversations/{run_id}")
def get_conversations(run_id: str):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT wc.*,
                      o.channel, o.sent_at as outreach_sent_at,
                      g.measure_key,
                      COALESCE(m.display_name, m.member_key) as display_name,
                      d.measure_name as measure_display_name
               FROM whatsapp_conversations wc
               LEFT JOIN fact_nba_outreach_plan o ON o.contact_id = wc.contact_id
               LEFT JOIN fact_member_gap g ON g.member_gap_key = wc.member_gap_key
               LEFT JOIN dim_member m ON m.member_key = wc.member_key
               LEFT JOIN dim_measure d ON d.measure_key = g.measure_key
               WHERE wc.nba_run_id = ?
               ORDER BY wc.last_updated DESC""",
            (run_id,)
        ).fetchall()
        convs = [dict(r) for r in rows]
        # patch display_name into measure_name field used by front-end
        for c in convs:
            if not c.get("display_name"):
                c["display_name"] = c.get("member_key", "")
            if c.get("measure_display_name"):
                c["measure_name"] = c["measure_display_name"]
        by_state = {}
        for c in convs:
            s = c.get("conversation_state", "UNKNOWN")
            by_state[s] = by_state.get(s, 0) + 1
    return {
        "run_id": run_id,
        "total": len(convs),
        "by_state": by_state,
        "conversations": convs,
    }


@app.post("/conversations/send-followups")
def send_conversation_followups():
    return {"sent": _send_conversation_followups()}


@app.post("/conversations/simulate")
def simulate_conversation(payload: dict):
    """Process a simulated member reply through the same state machine as the real webhook.
    Accepts {contact_id, reply} and returns {reply_msg, conversation_state, conversation}."""
    contact_id = payload.get("contact_id","")
    body_raw    = (payload.get("reply","") or "").strip().lower()
    now_iso     = datetime.now().isoformat(timespec="seconds")

    with get_db() as conn:
        conv = conn.execute(
            "SELECT * FROM whatsapp_conversations WHERE contact_id=? ORDER BY last_updated DESC LIMIT 1",
            (contact_id,)
        ).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="No conversation found for this contact")

    conv    = dict(conv)
    state   = conv["conversation_state"]
    conv_id = conv["conversation_id"]
    run_id  = conv["nba_run_id"]
    mgk     = conv["member_gap_key"]
    cid     = conv["contact_id"]
    measure = conv["measure_name"] or "health screening"

    reply_msg     = ""
    new_state     = state
    gap_closed    = False
    eval_triggered = False

    if state == "OUTREACH_SENT":
        if _contains_any(body_raw, _YES_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='AWAITING_DATE',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "Great! What date are you planning to go? Please reply with the date (e.g. July 15)"
            new_state = "AWAITING_DATE"
        elif _contains_any(body_raw, _NO_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='DECLINED',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "No problem! We will check back with you in 2 weeks. Your health matters to us."
            new_state = "DECLINED"
        else:
            reply_msg = f"Hi! Are you planning to schedule your {measure} appointment? Reply YES to let us know or NO if you need more time."

    elif state == "AWAITING_DATE":
        parsed = _parse_date_from_text(body_raw)
        if parsed:
            with get_db() as conn:
                conn.execute(
                    """UPDATE whatsapp_conversations
                       SET conversation_state='DATE_CONFIRMED', appointment_date=?, last_inbound_msg=?, last_updated=?
                       WHERE conversation_id=?""",
                    (parsed, body_raw, now_iso, conv_id)
                )
            try:
                friendly = date.fromisoformat(parsed).strftime("%B %d").lstrip("0").replace(" 0"," ")
            except Exception:
                friendly = parsed
            reply_msg = f"Perfect! We have noted your appointment for {friendly}. We will check in with you a few days after to see how it went. Good luck!"
            new_state = "DATE_CONFIRMED"
        else:
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "Thanks! Could you share the date you are planning to go? For example: July 15"

    elif state in ("FOLLOW_UP_SENT", "DATE_CONFIRMED"):
        # In simulator, DATE_CONFIRMED can also receive completion reply after fast-forward
        if _contains_any(body_raw, _DONE_WORDS):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='COMPLETED',gap_closed=1,last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
                if mgk:
                    conn.execute("UPDATE fact_member_gap SET gap_status='Closed' WHERE member_gap_key=?", (mgk,))
                if cid:
                    conn.execute("UPDATE fact_nba_outreach_plan SET status='COMPLETED' WHERE contact_id=?", (cid,))
                conn.execute(
                    """INSERT INTO fact_nba_trace (nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run_id, now_iso, "WhatsAppAgent", "GAP_CLOSED",
                     f"Gap {mgk} closed via simulated WhatsApp conversation",
                     "Member confirmed completion — gap_status set to Closed", 1)
                )
            reply_msg = "That is wonderful news! Thank you for taking care of your health. Your care team has been notified."
            new_state = "COMPLETED"
            gap_closed = True
            try: _run_evaluation(run_id)
            except Exception: pass
            eval_triggered = True
        elif _contains_any(body_raw, _NOT_YET):
            with get_db() as conn:
                conn.execute(
                    "UPDATE whatsapp_conversations SET conversation_state='DECLINED',last_inbound_msg=?,last_updated=? WHERE conversation_id=?",
                    (body_raw, now_iso, conv_id)
                )
            reply_msg = "No worries! Would you like to schedule another appointment? Reply YES to get a new reminder or STOP to opt out."
            new_state = "DECLINED"
        else:
            reply_msg = f"Thanks for your reply! Did you manage to complete your {measure} appointment? Reply YES if done or NO if not yet."
    else:
        reply_msg = f"Thanks for your message! Your care team has been notified."

    # Return updated conversation row
    with get_db() as conn:
        updated = conn.execute(
            "SELECT * FROM whatsapp_conversations WHERE conversation_id=?", (conv_id,)
        ).fetchone()

    return {
        "reply_msg": reply_msg,
        "conversation_state": new_state,
        "gap_closed": gap_closed,
        "eval_triggered": eval_triggered,
        "conversation": dict(updated) if updated else conv,
    }


@app.post("/conversations/fast-forward-followup")
def fast_forward_followup(payload: dict):
    """Immediately send the follow-up check-in message for a DATE_CONFIRMED conversation
    (simulator only — skips the 3-day wait)."""
    contact_id = payload.get("contact_id","")
    now_iso    = datetime.now().isoformat(timespec="seconds")

    with get_db() as conn:
        conv = conn.execute(
            "SELECT * FROM whatsapp_conversations WHERE contact_id=? ORDER BY last_updated DESC LIMIT 1",
            (contact_id,)
        ).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="No conversation found for this contact")

    conv = dict(conv)
    measure = conv.get("measure_name") or "health screening"
    msg = (f"Hi! We wanted to check in — did you manage to complete your "
           f"{measure} appointment? Reply YES if completed or NO if not yet.")

    with get_db() as conn:
        conn.execute(
            """UPDATE whatsapp_conversations
               SET conversation_state='FOLLOW_UP_SENT', follow_up_sent=1, last_updated=?
               WHERE conversation_id=?""",
            (now_iso, conv["conversation_id"])
        )

    return {"follow_up_msg": msg, "conversation_state": "FOLLOW_UP_SENT"}


def _send_conversation_followups() -> int:
    today_s = str(date.today())
    now_iso = datetime.now().isoformat(timespec="seconds")
    sent = 0
    with get_db() as conn:
        due = conn.execute(
            """SELECT * FROM whatsapp_conversations
               WHERE conversation_state = 'DATE_CONFIRMED'
                 AND follow_up_sent = 0
                 AND appointment_date IS NOT NULL
                 AND date(appointment_date, '+3 days') <= ?""",
            (today_s,)
        ).fetchall()

    for conv in due:
        conv = dict(conv)
        measure = conv.get("measure_name") or "health screening"
        msg = (f"Hi! We wanted to check in — did you manage to complete your "
               f"{measure} appointment? Reply YES if completed or NO if not yet.")
        _wa_reply(conv["member_phone"], msg)
        with get_db() as conn:
            conn.execute(
                """UPDATE whatsapp_conversations
                   SET conversation_state='FOLLOW_UP_SENT', follow_up_sent=1, last_updated=?
                   WHERE conversation_id=?""",
                (now_iso, conv["conversation_id"])
            )
            conn.execute(
                """INSERT INTO fact_nba_trace (nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count)
                   VALUES (?,?,?,?,?,?,?)""",
                (conv["nba_run_id"], now_iso, "WhatsAppAgent", "FOLLOWUP_SENT",
                 f"Follow-up sent for gap {conv['member_gap_key']}",
                 f"Appointment date was {conv['appointment_date']} — checking completion", 1)
            )
        sent += 1

    # Also escalate conversations with no follow-up reply after 7 days
    with get_db() as conn:
        stale = conn.execute(
            """SELECT * FROM whatsapp_conversations
               WHERE conversation_state = 'FOLLOW_UP_SENT'
                 AND date(last_updated, '+7 days') <= ?""",
            (today_s,)
        ).fetchall()
    for conv in stale:
        conv = dict(conv)
        with get_db() as conn:
            conn.execute(
                "UPDATE whatsapp_conversations SET conversation_state='ESCALATED',last_updated=? WHERE conversation_id=?",
                (now_iso, conv["conversation_id"])
            )

    return sent


# ── AI Agentic Loop Endpoints ─────────────────────────────────────────────────

import threading as _threading
from fastapi import BackgroundTasks

_analysis_status: dict[str, dict] = {}
_loop_status: dict[str, dict] = {}


def _bg_analyze(nba_run_id: str):
    try:
        from agent_loop import run_opportunity_analysis
        _analysis_status[nba_run_id] = {"status": "running", "started_at": datetime.now().isoformat()}
        result = run_opportunity_analysis(nba_run_id)
        _analysis_status[nba_run_id] = {**result, "finished_at": datetime.now().isoformat()}
    except Exception as e:
        _analysis_status[nba_run_id] = {"status": "error", "error": str(e), "finished_at": datetime.now().isoformat()}


@app.post("/analyze/{nba_run_id}", status_code=202)
def trigger_analysis(nba_run_id: str, background_tasks: BackgroundTasks):
    """Kick off an AI opportunity analysis in the background."""
    from agent_loop import ANTHROPIC_API_KEY as _ak
    if not _ak:
        raise HTTPException(
            status_code=503,
            detail="AI analysis unavailable: ANTHROPIC_API_KEY not set in .env"
        )
    if _analysis_status.get(nba_run_id, {}).get("status") == "running":
        return {"nba_run_id": nba_run_id, "status": "already_running"}
    _analysis_status[nba_run_id] = {"status": "queued", "queued_at": datetime.now().isoformat()}
    background_tasks.add_task(_bg_analyze, nba_run_id)
    return {"nba_run_id": nba_run_id, "status": "queued"}


@app.get("/analyze/{nba_run_id}/status")
def get_analysis_status(nba_run_id: str):
    """Poll the status of an in-progress or completed AI analysis."""
    status = _analysis_status.get(nba_run_id)
    if status is None:
        # Check DB for persisted results
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM claude_opportunity_analysis WHERE nba_run_id=? ORDER BY roi_ratio DESC LIMIT 3",
                (nba_run_id,)
            ).fetchall()
        if rows:
            return {"nba_run_id": nba_run_id, "status": "complete", "top_opportunities": rows_as_dicts(rows)}
        raise HTTPException(status_code=404, detail=f"No analysis found for {nba_run_id}")
    return {"nba_run_id": nba_run_id, **status}


@app.get("/analyze/latest")
def get_latest_analysis():
    """Return the most recent completed AI opportunity analysis."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM claude_opportunity_analysis ORDER BY created_timestamp DESC LIMIT 10"
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="No AI analyses found")
    return rows_as_dicts(rows)


def _bg_session_loop(run_id: str, measure_key: str, plan_key: str, mode: str):
    try:
        from agent_loop import run_session_loop
        key = f"{run_id}:{mode}"
        _loop_status[key] = {"status": "running", "started_at": datetime.now().isoformat()}
        result = run_session_loop(run_id, measure_key, plan_key, mode)
        _loop_status[key] = {**result, "finished_at": datetime.now().isoformat()}
    except Exception as e:
        key = f"{run_id}:{mode}"
        _loop_status[key] = {"status": "error", "error": str(e), "finished_at": datetime.now().isoformat()}


@app.post("/session/start-loop", status_code=202)
def start_session_loop(body: dict, background_tasks: BackgroundTasks):
    """Start an AI-powered session loop phase (cohort | campaign | outreach)."""
    from agent_loop import ANTHROPIC_API_KEY as _ak
    if not _ak:
        raise HTTPException(
            status_code=503,
            detail="AI session loop unavailable: ANTHROPIC_API_KEY not set in .env"
        )
    run_id = body.get("run_id") or body.get("nba_run_id")
    measure_key = body.get("measure_key")
    plan_key = body.get("plan_key")
    mode = body.get("mode", "cohort")
    if not run_id or not measure_key or not plan_key:
        raise HTTPException(status_code=400, detail="run_id, measure_key, and plan_key are required")

    key = f"{run_id}:{mode}"
    _loop_status[key] = {"status": "queued", "queued_at": datetime.now().isoformat()}
    background_tasks.add_task(_bg_session_loop, run_id, measure_key, plan_key, mode)
    return {"run_id": run_id, "measure_key": measure_key, "plan_key": plan_key, "mode": mode, "status": "queued"}


@app.get("/session/{run_id}/loop-status")
def get_loop_status(run_id: str, mode: str = "cohort"):
    """Poll the status of an in-progress AI session loop phase."""
    key = f"{run_id}:{mode}"
    status = _loop_status.get(key)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No loop found for run {run_id} mode {mode}")
    return {"run_id": run_id, "mode": mode, **status}


@app.post("/session/{run_id}/confirm-phase")
def confirm_phase(run_id: str, body: dict):
    """Record phase confirmation and optionally trigger the next loop phase."""
    phase = body.get("phase")
    next_mode = body.get("next_mode")
    measure_key = body.get("measure_key")
    plan_key = body.get("plan_key")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO fact_nba_trace
            (nba_run_id, timestamp, agent, step, input_summary, output_summary, affected_population_count)
            VALUES (?,?,?,?,?,?,?)
        """, (
            run_id, datetime.now().isoformat(), "PlanManager",
            f"PHASE_CONFIRMED_{(phase or '').upper()}",
            f"Plan manager confirmed phase: {phase}",
            body.get("notes", ""), 0
        ))

    result = {"run_id": run_id, "phase_confirmed": phase, "status": "ok"}

    if next_mode and measure_key and plan_key:
        from agent_loop import ANTHROPIC_API_KEY as _ak
        if _ak:
            import threading
            t = threading.Thread(
                target=_bg_session_loop,
                args=(run_id, measure_key, plan_key, next_mode),
                daemon=True
            )
            t.start()
            result["next_mode_triggered"] = next_mode

    return result


# ── Data Source Upload Endpoints ──────────────────────────────────────────────

# Column fingerprints for auto-detecting which table a CSV maps to
_TABLE_FINGERPRINTS = {
    "fact_member_gap": {"member_gap_key", "gap_status", "nba_propensity_score"},
    "dim_member": {"member_key", "dob_year", "digital_literacy_segment"},
    "dim_member_channel_pref": {"member_key", "email_allowed", "sms_allowed", "preferred_channel"},
    "dim_measure": {"measure_key", "measure_code", "star_weight", "hedis_domain"},
    "dim_plan_contract": {"plan_key", "plan_name", "star_rating_current"},
}

def _detect_table(headers: list[str]) -> str | None:
    header_set = {h.strip().lower() for h in headers}
    for table, required in _TABLE_FINGERPRINTS.items():
        if required <= {h.lower() for h in header_set}:
            return table
    return None


def _ingest_csv(content: bytes, filename: str) -> dict:
    """Parse a CSV and upsert rows into the matching DB table. Returns summary."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    table = _detect_table(headers)

    if not table:
        return {"status": "error", "message": f"Cannot identify table from columns: {headers[:8]}. Expected columns for fact_member_gap, dim_member, dim_measure, dim_plan_contract, or dim_member_channel_pref."}

    rows = list(reader)
    if not rows:
        return {"status": "error", "message": "CSV has no data rows."}

    cols = [h.strip() for h in headers]
    placeholders = ",".join(["?" for _ in cols])
    col_list = ",".join(cols)

    with get_db() as conn:
        # Clear existing data for this table before loading new data
        conn.execute(f"DELETE FROM {table}")
        inserted = 0
        for row in rows:
            vals = [row.get(c, None) for c in cols]
            try:
                conn.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", vals)
                inserted += 1
            except Exception:
                pass

    return {"status": "ok", "table": table, "rows_loaded": inserted, "filename": filename}


def _ingest_sqlite(content: bytes, filename: str) -> dict:
    """Copy matching tables from an uploaded SQLite DB into careintel.db."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()

        src = sqlite3.connect(tmp.name)
        src.row_factory = sqlite3.Row
        tables_loaded = {}

        for table in _TABLE_FINGERPRINTS.keys():
            try:
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    continue
                cols = rows[0].keys()
                placeholders = ",".join(["?" for _ in cols])
                col_list = ",".join(cols)
                with get_db() as conn:
                    conn.execute(f"DELETE FROM {table}")
                    for row in rows:
                        conn.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})", list(row))
                tables_loaded[table] = len(rows)
            except Exception as e:
                tables_loaded[table] = f"skipped: {e}"

        src.close()
        return {"status": "ok", "tables_loaded": tables_loaded, "filename": filename}
    finally:
        os.unlink(tmp.name)


_upload_status: dict = {}


def _bg_ingest_and_analyze(files_data: list[tuple[str, bytes]], run_id: str):
    """Background: ingest all uploaded files then auto-run AI opportunity analysis."""
    _upload_status[run_id] = {"phase": "ingesting", "results": [], "started_at": datetime.now().isoformat()}
    results = []

    for filename, content in files_data:
        if filename.lower().endswith(".db") or filename.lower().endswith(".sqlite") or filename.lower().endswith(".sqlite3"):
            r = _ingest_sqlite(content, filename)
        else:
            r = _ingest_csv(content, filename)
        results.append(r)

    errors = [r for r in results if r.get("status") == "error"]
    if errors:
        _upload_status[run_id] = {"phase": "failed", "results": results, "finished_at": datetime.now().isoformat()}
        return

    _upload_status[run_id] = {"phase": "analyzing", "results": results, "started_at": datetime.now().isoformat()}

    # Auto-run AI opportunity analysis on the new data
    try:
        from agent_loop import run_opportunity_analysis
        analysis = run_opportunity_analysis(run_id)
        _upload_status[run_id] = {
            "phase": "complete",
            "results": results,
            "analysis": analysis,
            "finished_at": datetime.now().isoformat(),
        }
        # Also store in the in-memory analysis cache so the dashboard can poll it
        _analysis_status[run_id] = {**analysis, "finished_at": datetime.now().isoformat()}
    except Exception as e:
        _upload_status[run_id] = {
            "phase": "complete",
            "results": results,
            "analysis": {"status": "error", "error": str(e)},
            "finished_at": datetime.now().isoformat(),
        }


@app.post("/datasource/upload")
async def upload_datasource(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    """
    Accept one or more CSV or SQLite files. Auto-detect table mapping, ingest,
    then automatically run Claude AI opportunity analysis on the new data.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    # Read all file contents eagerly (can't do async in background task)
    files_data = []
    for f in files:
        content = await f.read()
        files_data.append((f.filename or "upload", content))

    run_id = f"RUN_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _upload_status[run_id] = {"phase": "queued", "queued_at": datetime.now().isoformat()}
    background_tasks.add_task(_bg_ingest_and_analyze, files_data, run_id)

    return {"run_id": run_id, "files_received": [f[0] for f in files_data], "status": "queued"}


@app.get("/datasource/status/{run_id}")
def datasource_status(run_id: str):
    """Poll the status of a datasource upload + analysis job."""
    status = _upload_status.get(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No upload job found for {run_id}")
    return {"run_id": run_id, **status}


@app.get("/datasource/tables")
def datasource_tables():
    """Return row counts for all core data tables — lets the UI confirm data is loaded."""
    with get_db() as conn:
        counts = {}
        for table in [*_TABLE_FINGERPRINTS.keys(), "claude_opportunity_analysis", "fact_nba_claude_decision"]:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[table] = 0
    return counts
