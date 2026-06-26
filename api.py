#!/usr/bin/env python3
"""CareIntel NBA FastAPI — connects to careintel.db."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db")

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


# ── POST /session/start ───────────────────────────────────────────────────────

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
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM fact_member_gap
            WHERE measure_key = ?
              AND plan_key = ?
              AND LOWER(gap_status) IN ('open', 'borderline')
              AND LOWER(is_suppressed) != 'true'
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
