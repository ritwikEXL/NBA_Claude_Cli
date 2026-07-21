#!/usr/bin/env python3
"""CareIntel Agentic Loop — real Claude AI reasoning via Anthropic Python SDK."""

import os
import json
import sqlite3
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db")
MODEL = "anthropic/claude-sonnet-4-6"  # OpenRouter model ID format

# ── Measure and plan constants ────────────────────────────────────────────────

MEASURE_WEIGHTS = {
    "M001": 1.0,  # BCS
    "M002": 1.5,  # COL
    "M003": 2.0,  # EED
    "M004": 2.0,  # CDC
    "M005": 3.0,  # MAD
    "M006": 1.0,  # AFV
    "M007": 2.0,  # SPC
}

PLAN_MEMBERS = {
    "P001": 1500, "P002": 1200, "P003": 1000, "P004": 1800, "P005": 500,
}

PLAN_PMPM = {
    "P001": 1050.0, "P002": 1120.0, "P003": 980.0, "P004": 1180.0, "P005": 920.0,
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_analysis_table():
    conn = _db_conn()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS claude_opportunity_analysis (
            analysis_id            TEXT PRIMARY KEY,
            nba_run_id             TEXT,
            measure_key            TEXT,
            plan_key               TEXT,
            compliance_rate        REAL,
            benchmark_gap          REAL,
            stars_impact           REAL,
            cms_bonus_impact       REAL,
            total_outreach_cost    REAL,
            expected_closures      INTEGER,
            roi_ratio              REAL,
            recommended_tier_strategy TEXT,
            claude_reasoning       TEXT,
            confidence_level       TEXT,
            created_timestamp      TEXT
        )
        """)
        conn.commit()
    finally:
        conn.close()


_ensure_analysis_table()


# ── Client factory (graceful fallback) ────────────────────────────────────────

def get_client():
    """Return True if API key is set (we call OpenRouter directly via requests)."""
    return bool(ANTHROPIC_API_KEY)


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "query_opportunities",
        "description": (
            "Query open HEDIS/Stars gaps from the database. Returns measure × plan combinations "
            "with open gap counts, average propensity scores, compliance rates, and plan metadata "
            "(members, PMPM, current Stars rating, target Stars rating). Use this to identify "
            "the most promising opportunity areas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {
                    "type": "string",
                    "description": "Optional filter: M001-M007. Omit to get all measures."
                },
                "plan_key": {
                    "type": "string",
                    "description": "Optional filter: P001-P005. Omit to get all plans."
                },
                "min_open_gaps": {
                    "type": "integer",
                    "description": "Minimum open gap count to include (default 1)."
                }
            },
            "required": []
        }
    },
    {
        "name": "query_members",
        "description": (
            "Query member-level details for a specific measure × plan combination. Returns member "
            "demographics, digital literacy, channel preferences, propensity scores, and gap status. "
            "Use for cohort segmentation analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string", "description": "Measure key, e.g. M001"},
                "plan_key": {"type": "string", "description": "Plan key, e.g. P001"},
                "gap_status_filter": {
                    "type": "string",
                    "description": "Filter by gap status: 'open', 'borderline', 'open_or_borderline'. Default: open_or_borderline."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 200)."
                }
            },
            "required": ["measure_key", "plan_key"]
        }
    },
    {
        "name": "write_opportunity_analysis",
        "description": (
            "Persist Claude's opportunity analysis for a single measure × plan combination into "
            "the claude_opportunity_analysis table. Call this after calculating financial impact "
            "for each opportunity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nba_run_id": {"type": "string"},
                "measure_key": {"type": "string"},
                "plan_key": {"type": "string"},
                "compliance_rate": {"type": "number", "description": "Current compliance rate 0-1"},
                "benchmark_gap": {"type": "number", "description": "Gap to national avg benchmark (positive = below avg)"},
                "stars_impact": {"type": "number", "description": "Projected Stars improvement from closing all gaps"},
                "cms_bonus_impact": {"type": "number", "description": "Projected CMS bonus $ from Stars improvement"},
                "total_outreach_cost": {"type": "number", "description": "Estimated total outreach cost $"},
                "expected_closures": {"type": "integer", "description": "Expected gap closures from outreach"},
                "roi_ratio": {"type": "number", "description": "ROI = cms_bonus_impact / total_outreach_cost"},
                "recommended_tier_strategy": {"type": "string", "description": "e.g. 'Tier-1 digital-first with Tier-2 follow-up'"},
                "claude_reasoning": {"type": "string", "description": "Claude's plain-English rationale for this opportunity ranking"},
                "confidence_level": {"type": "string", "description": "HIGH, MEDIUM, or LOW"}
            },
            "required": [
                "nba_run_id", "measure_key", "plan_key", "compliance_rate", "benchmark_gap",
                "stars_impact", "cms_bonus_impact", "total_outreach_cost", "expected_closures",
                "roi_ratio", "recommended_tier_strategy", "claude_reasoning", "confidence_level"
            ]
        }
    },
    {
        "name": "write_session_decision",
        "description": (
            "Write or update NBA decision rows in fact_nba_claude_decision for members in a "
            "specific cohort. Call this during session phases 2-3 to record cohort assignments, "
            "channel choices, incentive decisions, and expected lift."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nba_run_id": {"type": "string"},
                "measure_key": {"type": "string"},
                "plan_key": {"type": "string"},
                "cohort_id": {"type": "string"},
                "cohort_name": {"type": "string"},
                "nba_action_type": {"type": "string", "description": "e.g. OUTREACH_SMS, OUTREACH_CALL, OUTREACH_EMAIL"},
                "final_channel": {"type": "string"},
                "final_incentive": {"type": "string", "description": "e.g. GIFTCARD_25, NONE"},
                "priority_score": {"type": "number"},
                "expected_gap_closure_lift": {"type": "number", "description": "0-1 closure probability"},
                "explanation_text": {"type": "string"},
                "member_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of member_keys to assign to this cohort decision."
                }
            },
            "required": [
                "nba_run_id", "measure_key", "plan_key", "cohort_id", "cohort_name",
                "nba_action_type", "final_channel", "final_incentive", "priority_score",
                "expected_gap_closure_lift", "explanation_text"
            ]
        }
    },
    {
        "name": "write_trace",
        "description": "Append an audit trace row to fact_nba_trace for the current run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nba_run_id": {"type": "string"},
                "agent": {"type": "string", "description": "e.g. OpportunityAgent, SegmentationAgent, CampaignAgent, OutreachAgent"},
                "step": {"type": "string", "description": "Short step label, e.g. OPPORTUNITY_SELECTED"},
                "input_summary": {"type": "string"},
                "output_summary": {"type": "string"},
                "affected_population_count": {"type": "integer"}
            },
            "required": ["nba_run_id", "agent", "step", "input_summary", "output_summary"]
        }
    },
    {
        "name": "get_historical_performance",
        "description": (
            "Retrieve historical outreach closure rates and campaign performance from prior NBA runs. "
            "Returns measure-level closure rate actuals vs expected, and evaluation scores if available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string", "description": "Optional filter by measure."},
                "plan_key": {"type": "string", "description": "Optional filter by plan."}
            },
            "required": []
        }
    },
    {
        "name": "calculate_financial_impact",
        "description": (
            "Calculate CMS bonus impact, ROI, and outreach cost for a measure × plan opportunity. "
            "Uses the PMPM formula: stars_delta × plan_members × (plan_pmpm × 12) × 0.05. "
            "Applies per-campaign Stars cap of star_weight × 0.15 and portfolio cap of 0.30."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string"},
                "plan_key": {"type": "string"},
                "open_gaps": {"type": "integer", "description": "Number of open/borderline gaps"},
                "tier1_count": {"type": "integer", "description": "Members eligible for Tier-1 outreach"},
                "tier2_count": {"type": "integer", "description": "Members eligible for Tier-2 outreach"},
                "tier3_count": {"type": "integer", "description": "Members eligible for Tier-3 outreach"}
            },
            "required": ["measure_key", "plan_key", "open_gaps"]
        }
    }
]


# ── Tool execution ────────────────────────────────────────────────────────────

def _execute_tool(name: str, inputs: dict) -> Any:
    if name == "query_opportunities":
        return _tool_query_opportunities(**inputs)
    if name == "query_members":
        return _tool_query_members(**inputs)
    if name == "write_opportunity_analysis":
        return _tool_write_opportunity_analysis(**inputs)
    if name == "write_session_decision":
        return _tool_write_session_decision(**inputs)
    if name == "write_trace":
        return _tool_write_trace(**inputs)
    if name == "get_historical_performance":
        return _tool_get_historical_performance(**inputs)
    if name == "calculate_financial_impact":
        return _tool_calculate_financial_impact(**inputs)
    return {"error": f"Unknown tool: {name}"}


def _tool_query_opportunities(measure_key=None, plan_key=None, min_open_gaps=1):
    conn = _db_conn()
    try:
        where_clauses = [
            "LOWER(g.gap_status) IN ('open','borderline')",
            "LOWER(g.is_suppressed) != 'true'",
        ]
        params = []
        if measure_key:
            where_clauses.append("g.measure_key = ?")
            params.append(measure_key)
        if plan_key:
            where_clauses.append("g.plan_key = ?")
            params.append(plan_key)

        where_sql = " AND ".join(where_clauses)

        rows = conn.execute(f"""
            SELECT
                m.measure_key, m.measure_code, m.measure_name,
                m.star_weight, m.hedis_domain,
                p.plan_key, p.plan_name, p.region, p.segment,
                p.total_members, p.plan_pmpm_monthly,
                p.star_rating_current, p.star_rating_target,
                COUNT(DISTINCT CASE WHEN {where_sql} THEN g.member_key END) AS open_gaps,
                COUNT(DISTINCT g.member_key) AS total_eligible,
                SUM(CASE WHEN LOWER(g.gap_status)='closed' THEN 1 ELSE 0 END) AS closed_gaps,
                ROUND(AVG(CASE WHEN {where_sql} THEN g.nba_propensity_score END), 4) AS avg_propensity
            FROM fact_member_gap g
            JOIN dim_measure       m ON m.measure_key = g.measure_key
            JOIN dim_plan_contract p ON p.plan_key    = g.plan_key
            GROUP BY m.measure_key, p.plan_key
            HAVING open_gaps >= ?
        """, params * 2 + [min_open_gaps]).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["compliance_rate"] = round(d["closed_gaps"] / d["total_eligible"], 4) if d["total_eligible"] else 0
            result.append(d)
        result.sort(key=lambda x: x["star_weight"] * x["open_gaps"], reverse=True)
        return result
    finally:
        conn.close()


def _tool_query_members(measure_key, plan_key, gap_status_filter="open_or_borderline", limit=200):
    conn = _db_conn()
    try:
        if gap_status_filter == "open_or_borderline":
            status_sql = "LOWER(g.gap_status) IN ('open','borderline')"
        else:
            status_sql = f"LOWER(g.gap_status) = '{gap_status_filter.lower()}'"

        rows = conn.execute(f"""
            SELECT
                g.member_gap_key, g.member_key, g.gap_status, g.days_open,
                g.nba_propensity_score,
                mb.age, mb.gender, mb.language_preference, mb.digital_literacy_segment,
                mb.socioeconomic_segment,
                cp.preferred_channel, cp.email_opt_in, cp.sms_opt_in, cp.call_opt_in,
                cp.do_not_contact
            FROM fact_member_gap g
            JOIN dim_member mb ON mb.member_key = g.member_key
            LEFT JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
            WHERE g.measure_key = ? AND g.plan_key = ?
              AND {status_sql}
              AND LOWER(g.is_suppressed) != 'true'
            ORDER BY g.nba_propensity_score DESC
            LIMIT ?
        """, (measure_key, plan_key, limit)).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _tool_write_opportunity_analysis(
    nba_run_id, measure_key, plan_key, compliance_rate, benchmark_gap,
    stars_impact, cms_bonus_impact, total_outreach_cost, expected_closures,
    roi_ratio, recommended_tier_strategy, claude_reasoning, confidence_level
):
    conn = _db_conn()
    try:
        analysis_id = f"ANA_{nba_run_id}_{measure_key}_{plan_key}"
        ts = datetime.now().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO claude_opportunity_analysis
            (analysis_id, nba_run_id, measure_key, plan_key, compliance_rate,
             benchmark_gap, stars_impact, cms_bonus_impact, total_outreach_cost,
             expected_closures, roi_ratio, recommended_tier_strategy,
             claude_reasoning, confidence_level, created_timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            analysis_id, nba_run_id, measure_key, plan_key, compliance_rate,
            benchmark_gap, stars_impact, cms_bonus_impact, total_outreach_cost,
            expected_closures, roi_ratio, recommended_tier_strategy,
            claude_reasoning, confidence_level, ts
        ))
        conn.commit()
        return {"status": "ok", "analysis_id": analysis_id}
    finally:
        conn.close()


def _tool_write_session_decision(
    nba_run_id, measure_key, plan_key, cohort_id, cohort_name,
    nba_action_type, final_channel, final_incentive,
    priority_score, expected_gap_closure_lift, explanation_text,
    member_keys=None
):
    conn = _db_conn()
    try:
        ts = datetime.now().isoformat()
        if not member_keys:
            member_keys = []

        written = 0
        for mk in member_keys:
            rows = conn.execute("""
                SELECT member_gap_key FROM fact_member_gap
                WHERE member_key = ? AND measure_key = ? AND plan_key = ?
                  AND LOWER(gap_status) IN ('open','borderline')
            """, (mk, measure_key, plan_key)).fetchall()

            for row in rows:
                mgk = row["member_gap_key"]
                conn.execute("""
                    INSERT OR REPLACE INTO fact_nba_claude_decision
                    (nba_run_id, member_gap_key, measure_key, plan_key,
                     cohort_id, cohort_name, nba_action_type,
                     final_channel, final_incentive, priority_score,
                     expected_gap_closure_lift, explanation_text, created_timestamp)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    nba_run_id, mgk, measure_key, plan_key,
                    cohort_id, cohort_name, nba_action_type,
                    final_channel, final_incentive, priority_score,
                    expected_gap_closure_lift, explanation_text, ts
                ))
                written += 1

        conn.commit()
        return {"status": "ok", "rows_written": written, "cohort_id": cohort_id}
    finally:
        conn.close()


def _tool_write_trace(
    nba_run_id, agent, step, input_summary, output_summary, affected_population_count=0
):
    conn = _db_conn()
    try:
        ts = datetime.now().isoformat()
        conn.execute("""
            INSERT INTO fact_nba_trace
            (nba_run_id, timestamp, agent, step, input_summary, output_summary, affected_population_count)
            VALUES (?,?,?,?,?,?,?)
        """, (nba_run_id, ts, agent, step, input_summary, output_summary, affected_population_count))
        conn.commit()
        return {"status": "ok", "timestamp": ts}
    finally:
        conn.close()


def _tool_get_historical_performance(measure_key=None, plan_key=None):
    conn = _db_conn()
    try:
        where = []
        params = []
        if measure_key:
            where.append("ce.measure_code = (SELECT measure_code FROM dim_measure WHERE measure_key = ?)")
            params.append(measure_key)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(f"""
            SELECT ce.measure_code, ce.campaign_id, ce.evaluation_date,
                   ce.total_members_contacted, ce.gaps_closed_actual, ce.gaps_closed_expected,
                   ce.actual_closure_rate, ce.expected_closure_rate, ce.performance_status,
                   ce.stars_impact_actual, ce.stars_impact_projected
            FROM campaign_evaluations ce
            {where_sql}
            ORDER BY ce.evaluation_date DESC
            LIMIT 50
        """, params).fetchall()

        return [dict(r) for r in rows] if rows else []
    finally:
        conn.close()


def _tool_calculate_financial_impact(
    measure_key, plan_key, open_gaps, tier1_count=0, tier2_count=0, tier3_count=0
):
    star_weight = MEASURE_WEIGHTS.get(measure_key, 1.0)
    plan_members = PLAN_MEMBERS.get(plan_key, 1000)
    pmpm = PLAN_PMPM.get(plan_key, 1050.0)

    # Closure estimates by tier
    t1_closures = round(tier1_count * 0.60)
    t2_closures = round(tier2_count * 0.35)
    t3_closures = round(tier3_count * 0.18)
    remaining = open_gaps - tier1_count - tier2_count - tier3_count
    remaining_closures = round(max(remaining, 0) * 0.12)
    expected_closures = t1_closures + t2_closures + t3_closures + remaining_closures

    # Stars impact capped per-campaign and portfolio
    if plan_members > 0:
        stars_delta_raw = (expected_closures / plan_members) * star_weight
    else:
        stars_delta_raw = 0.0
    per_campaign_cap = star_weight * 0.15
    stars_delta = min(stars_delta_raw, per_campaign_cap)
    stars_delta = min(stars_delta, 0.30)  # portfolio cap

    # CMS bonus: delta × members × annual_pmpm × 5%
    annual_pmpm = pmpm * 12
    cms_bonus = round(stars_delta * plan_members * annual_pmpm * 0.05, 2)

    # Outreach costs
    t1_cost = tier1_count * 0.50
    t2_cost = tier2_count * 15.50
    t3_cost = tier3_count * 25.10
    remaining_cost = max(remaining, 0) * 0.50
    total_cost = round(t1_cost + t2_cost + t3_cost + remaining_cost, 2)

    roi = round(cms_bonus / total_cost, 2) if total_cost > 0 else 0.0

    return {
        "measure_key": measure_key,
        "plan_key": plan_key,
        "star_weight": star_weight,
        "plan_members": plan_members,
        "open_gaps": open_gaps,
        "expected_closures": expected_closures,
        "stars_delta": round(stars_delta, 4),
        "cms_bonus_impact": cms_bonus,
        "total_outreach_cost": total_cost,
        "roi_ratio": roi,
        "tier1_closures": t1_closures,
        "tier2_closures": t2_closures,
        "tier3_closures": t3_closures,
    }


# ── Agentic loop runner ───────────────────────────────────────────────────────

def _openrouter_request(messages: list, tools: list) -> dict:
    """POST to OpenRouter chat completions endpoint. Returns parsed JSON response."""
    import requests as _req

    # Convert Anthropic tool schema format to OpenAI function format
    openai_tools = []
    for t in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            }
        })

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": messages,
        "tools": openai_tools,
        "tool_choice": "auto",
    }

    headers = {
        "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://careintel.local",
        "X-Title": "CareIntel NBA",
    }

    resp = _req.post(OPENROUTER_BASE_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _run_loop(client, system_prompt: str, user_message: str, nba_run_id: str) -> dict:
    """Core while-tool_use loop using OpenRouter (OpenAI-compatible format)."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tool_calls_made = []
    final_text = ""

    while True:
        data = _openrouter_request(messages, TOOLS)

        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "stop")
        msg = choice["message"]

        # Collect text content
        if msg.get("content"):
            final_text = msg["content"]

        if finish_reason != "tool_calls" or not msg.get("tool_calls"):
            break

        # Append assistant message with tool_calls
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]})

        # Execute each tool call and collect results
        for tc in msg["tool_calls"]:
            tool_id = tc["id"]
            tool_name = tc["function"]["name"]
            try:
                tool_input = json.loads(tc["function"]["arguments"])
            except Exception:
                tool_input = {}

            result = _execute_tool(tool_name, tool_input)
            tool_calls_made.append({"tool": tool_name, "inputs": tool_input, "result": result})

            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": json.dumps(result),
            })

    return {"final_text": final_text, "tool_calls": tool_calls_made}


# ── Phase 1: Opportunity Analysis ─────────────────────────────────────────────

def run_opportunity_analysis(nba_run_id: str) -> dict:
    """
    Phase 1 — Opportunity Agent mode.
    Scans all measure × plan combinations, computes financial impact, writes
    analysis rows, writes trace. Returns ranked opportunities.
    """
    if not get_client():
        return {
            "status": "unavailable",
            "error": "ANTHROPIC_API_KEY not configured",
            "message": "AI analysis is unavailable. Set ANTHROPIC_API_KEY in .env to enable."
        }

    system_prompt = """You are the CareIntel Opportunity Agent — an expert Medicare Stars analyst.

Follow these steps IN ORDER, one tool call at a time:
1. Call write_trace with step=ANALYSIS_STARTED
2. Call query_opportunities (no filters) to get all open gaps by measure × plan
3. From the results, pick the TOP 5 by (star_weight × open_gaps) — do NOT call calculate_financial_impact for all 35
4. For each of those top 5, call calculate_financial_impact (pass open_gaps; estimate tier1_count=60% of open_gaps, tier2_count=30%, tier3_count=10%)
5. For each of those top 5, call write_opportunity_analysis with: compliance_rate=(closed_gaps/total_eligible), benchmark_gap=(national_avg - compliance_rate, use 0.74 for BCS/EED, 0.68 for COL, 0.72 for others), stars_impact from calculate_financial_impact result, cms_bonus_impact, total_outreach_cost, expected_closures, roi_ratio, recommended_tier_strategy (e.g. "Tier-1 SMS primary, Tier-2 call fallback"), claude_reasoning (2 sentences: why this is a good opportunity), confidence_level (HIGH if roi_ratio>5, LOW if roi_ratio<2, else MEDIUM)
6. Call write_trace with step=ANALYSIS_COMPLETE and output_summary listing the top 3 measure×plan combos by ROI

After all tool calls are done, respond with this exact JSON (no other text):
{"ranked_opportunities": [{"rank":1,"measure_key":"...","plan_key":"...","roi_ratio":0.0},{"rank":2,...},{"rank":3,...}], "total_analyzed": 5, "analysis_complete": true}"""

    user_message = f"""Run opportunity analysis for NBA run {nba_run_id}. Use this nba_run_id in all write calls. Proceed step by step."""

    try:
        result = _run_loop(True, system_prompt, user_message, nba_run_id)
        final_text = result["final_text"]

        # Try to parse a JSON summary from the response
        try:
            import re
            json_match = re.search(r'\{[\s\S]*"ranked_opportunities"[\s\S]*\}', final_text)
            summary = json.loads(json_match.group()) if json_match else {}
        except Exception:
            summary = {}

        # Pull top results from DB as fallback
        conn = _db_conn()
        try:
            rows = conn.execute("""
                SELECT * FROM claude_opportunity_analysis
                WHERE nba_run_id = ?
                ORDER BY roi_ratio DESC LIMIT 10
            """, (nba_run_id,)).fetchall()
            db_results = [dict(r) for r in rows]
        finally:
            conn.close()

        return {
            "status": "complete",
            "nba_run_id": nba_run_id,
            "tool_calls_made": len(result["tool_calls"]),
            "top_opportunities": db_results[:3],
            "claude_summary": summary,
            "raw_response": final_text[:2000],
        }

    except Exception as e:
        return {
            "status": "error",
            "nba_run_id": nba_run_id,
            "error": str(e),
        }


# ── Phase 2-4: Session Loop ───────────────────────────────────────────────────

def run_session_loop(nba_run_id: str, measure_key: str, plan_key: str, mode: str) -> dict:
    """
    Multi-phase session agent loop.
    mode: 'cohort' | 'campaign' | 'outreach'
    Each mode runs one phase of the NBA wizard and writes relevant DB rows.
    """
    if not get_client():
        return {
            "status": "unavailable",
            "error": "ANTHROPIC_API_KEY not configured",
        }

    phase_prompts = {
        "cohort": {
            "system": """You are the CareIntel Segmentation Agent — expert at Medicare member segmentation.

Your job is to analyze member-level data for a specific measure × plan and propose 2-4 interpretable cohorts.

Steps:
1. Call query_members to get member demographics and channel preferences
2. Design 2-4 cohorts based on: propensity score, digital literacy, channel availability, language, days_open
3. Call write_session_decision for each cohort (use the top member_keys for each segment)
4. Call write_trace with step COHORT_DESIGNED

Return a JSON summary with keys: cohorts (list with cohort_id, cohort_name, size, rationale, channel, priority_rank), total_members_analyzed (int)""",
            "user": f"""Design cohorts for NBA run {nba_run_id}, measure {measure_key}, plan {plan_key}.

Query members, segment them into 2-4 meaningful cohorts, write decisions for each cohort, and trace your work.
Use nba_run_id: {nba_run_id} in all write calls."""
        },
        "campaign": {
            "system": """You are the CareIntel Campaign Design Agent — expert at Medicare outreach campaigns.

Your job is to design a campaign for approved cohorts: channel strategy, frequency, incentives, message themes.

Steps:
1. Call query_members to review member characteristics (check channel opt-ins)
2. Design a campaign: primary channel, fallback channel, frequency plan, incentive tier
3. Call write_session_decision to update decisions with nba_action_type, final_channel, final_incentive
4. Call write_trace with step CAMPAIGN_DESIGNED

Guidelines:
- Tier-1 (high propensity, digital): SMS or email primary, no incentive or $15 gift card
- Tier-2 (medium propensity): SMS + call fallback, $25 gift card
- Tier-3 (low propensity, language barrier): call primary, $25-$50 gift card, plain language

Return JSON with keys: campaign_id, channel_strategy, frequency_plan, incentive_strategy, message_theme, estimated_contacts (int)""",
            "user": f"""Design campaign for NBA run {nba_run_id}, measure {measure_key}, plan {plan_key}.

Query members, design an appropriate channel + incentive strategy, update decisions, and trace your work.
Use nba_run_id: {nba_run_id} in all write calls."""
        },
        "outreach": {
            "system": """You are the CareIntel Outreach Agent — responsible for generating the final outreach plan.

Your job is to translate the approved campaign into a concrete outreach schedule.

Steps:
1. Call query_members to get the final member list for outreach
2. Call write_trace with step OUTREACH_PLAN_GENERATED, including counts by channel
3. Summarize the plan: contacts by channel, timeline, expected Stars impact

Return JSON with keys: total_contacts (int), by_channel (dict), timeline_days (int), expected_gap_closures (int), expected_stars_lift (float)""",
            "user": f"""Generate outreach plan for NBA run {nba_run_id}, measure {measure_key}, plan {plan_key}.

Query the planned members, write the final trace entry, and return a concise outreach summary.
Use nba_run_id: {nba_run_id} in all write calls."""
        }
    }

    if mode not in phase_prompts:
        return {"status": "error", "error": f"Unknown mode: {mode}. Use cohort, campaign, or outreach."}

    prompts = phase_prompts[mode]

    try:
        result = _run_loop(True, prompts["system"], prompts["user"], nba_run_id)
        final_text = result["final_text"]

        try:
            import re
            json_match = re.search(r'\{[\s\S]*\}', final_text)
            summary = json.loads(json_match.group()) if json_match else {}
        except Exception:
            summary = {}

        return {
            "status": "complete",
            "nba_run_id": nba_run_id,
            "measure_key": measure_key,
            "plan_key": plan_key,
            "mode": mode,
            "tool_calls_made": len(result["tool_calls"]),
            "phase_summary": summary,
            "raw_response": final_text[:2000],
        }

    except Exception as e:
        return {
            "status": "error",
            "nba_run_id": nba_run_id,
            "mode": mode,
            "error": str(e),
        }
