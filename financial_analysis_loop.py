"""
CareIntel Financial Analysis Loop — agentic analysis using OpenRouter (OpenAI client).
The ANTHROPIC_API_KEY env var contains an OpenRouter key.
"""

from openai import OpenAI
from dotenv import load_dotenv
import sqlite3
import json
import os
import logging
from datetime import datetime

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv('ANTHROPIC_API_KEY'),
    default_headers={
        "HTTP-Referer": "https://careintel.exl.com",
        "X-Title": "CareIntel Financial Analysis"
    }
)

DB_PATH = os.getenv('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'careintel.db'))
MODEL = "anthropic/claude-opus-4-8"


# ── Database setup ─────────────────────────────────────────────────────────────

def ensure_financial_analyses_table(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS financial_analyses (
        analysis_id TEXT PRIMARY KEY,
        measure_key TEXT,
        plan_key TEXT,
        source_id TEXT DEFAULT 'demo',
        analysis_version TEXT,
        eligible_population INTEGER,
        current_compliance_rate REAL,
        national_benchmark REAL,
        gap_to_benchmark REAL,
        open_gaps_count INTEGER,
        tier_1_count INTEGER,
        tier_1_definition TEXT,
        tier_1_closure_rate REAL,
        tier_1_closure_rationale TEXT,
        tier_1_cost_per_member REAL,
        tier_1_cost_rationale TEXT,
        tier_1_expected_closures INTEGER,
        tier_2_count INTEGER,
        tier_2_definition TEXT,
        tier_2_closure_rate REAL,
        tier_2_closure_rationale TEXT,
        tier_2_cost_per_member REAL,
        tier_2_expected_closures INTEGER,
        tier_3_count INTEGER,
        tier_3_definition TEXT,
        tier_3_closure_rate REAL,
        tier_3_closure_rationale TEXT,
        tier_3_cost_per_member REAL,
        tier_3_expected_closures INTEGER,
        expected_total_closures INTEGER,
        stars_improvement REAL,
        stars_improvement_rationale TEXT,
        cms_bonus_impact REAL,
        total_outreach_cost REAL,
        net_return REAL,
        return_per_dollar REAL,
        confidence_level TEXT,
        confidence_rationale TEXT,
        key_risks TEXT,
        key_opportunities TEXT,
        recommended_approach TEXT,
        plain_english_summary TEXT,
        created_timestamp TEXT,
        claude_model_used TEXT
    )
    """)


# ── Tool functions ─────────────────────────────────────────────────────────────

def get_measure_data(measure_key=None, plan_key=None, source_id='demo'):
    """Pull all relevant data for a measure × plan combination."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    result = {}

    try:
        # Measure metadata
        if measure_key:
            m_row = conn.execute(
                "SELECT measure_key, measure_code, measure_name, star_weight, clinical_description FROM dim_measure WHERE measure_key=?",
                (measure_key,)
            ).fetchone()
            result['measure'] = dict(m_row) if m_row else {}
        else:
            result['measure'] = {}

        # Plan metadata
        if plan_key:
            p_row = conn.execute(
                """SELECT p.plan_key, p.plan_name, p.plan_annual_revenue, p.total_members,
                          p.star_rating_current, p.star_rating_target, p.region, p.segment,
                          pp.total_members AS pop_members, pp.plan_revenue AS pop_revenue
                   FROM dim_plan_contract p
                   LEFT JOIN plan_population pp ON pp.plan_key = p.plan_key
                   WHERE p.plan_key=?""",
                (plan_key,)
            ).fetchone()
            if p_row:
                pd = dict(p_row)
                pd['plan_annual_revenue'] = pd.get('pop_revenue') or pd.get('plan_annual_revenue') or 0
                pd['total_members'] = pd.get('pop_members') or pd.get('total_members') or 0
                result['plan'] = pd
            else:
                result['plan'] = {}
        else:
            result['plan'] = {}

        # Gap distribution
        gap_q = """
            SELECT
                COUNT(*) AS total_gaps,
                SUM(CASE WHEN LOWER(gap_status) IN ('open','borderline','partial') THEN 1 ELSE 0 END) AS open_gaps,
                SUM(CASE WHEN LOWER(gap_status) = 'closed' THEN 1 ELSE 0 END) AS closed_gaps,
                SUM(CASE WHEN LOWER(gap_status) = 'borderline' THEN 1 ELSE 0 END) AS borderline,
                SUM(CASE WHEN LOWER(gap_status) = 'partial' THEN 1 ELSE 0 END) AS partial
            FROM fact_member_gap
            WHERE (source_id=? OR source_id IS NULL)
        """
        params = [source_id]
        if measure_key:
            gap_q += " AND measure_key=?"
            params.append(measure_key)
        if plan_key:
            gap_q += " AND plan_key=?"
            params.append(plan_key)
        gd = conn.execute(gap_q, params).fetchone()
        result['gap_distribution'] = dict(gd) if gd else {}

        # Member profiles (digital_literacy, language, ses) for open gaps
        try:
            mp_rows = conn.execute("""
                SELECT m.digital_literacy_segment, m.language_preference, m.socioeconomic_segment, COUNT(*) AS cnt
                FROM fact_member_gap g
                JOIN dim_member m ON m.member_key = g.member_key
                WHERE LOWER(g.gap_status) IN ('open','borderline','partial')
                  AND (g.source_id=? OR g.source_id IS NULL)
                  {}{}
                GROUP BY m.digital_literacy_segment, m.language_preference, m.socioeconomic_segment
            """.format(
                "AND g.measure_key=?" if measure_key else "",
                "AND g.plan_key=?" if plan_key else ""
            ), [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchall()

            dig_lit = {}; lang_pref = {}; ses_seg = {}
            for r in mp_rows:
                d = dict(r)
                dig_lit[d['digital_literacy_segment']] = dig_lit.get(d['digital_literacy_segment'], 0) + d['cnt']
                lang_pref[d['language_preference']] = lang_pref.get(d['language_preference'], 0) + d['cnt']
                ses_seg[d['socioeconomic_segment']] = ses_seg.get(d['socioeconomic_segment'], 0) + d['cnt']
            result['member_profiles'] = {
                'digital_literacy': dig_lit,
                'language_preference': lang_pref,
                'socioeconomic_segment': ses_seg
            }
        except Exception:
            result['member_profiles'] = {}

        # Channel consent
        try:
            ch_rows = conn.execute("""
                SELECT
                    ROUND(AVG(CASE WHEN LOWER(cp.email_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_email_allowed,
                    ROUND(AVG(CASE WHEN LOWER(cp.sms_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_sms_allowed,
                    ROUND(AVG(CASE WHEN LOWER(cp.call_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_call_allowed
                FROM fact_member_gap g
                JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
                WHERE LOWER(g.gap_status) IN ('open','borderline','partial')
                  AND (g.source_id=? OR g.source_id IS NULL)
                  {}{}
            """.format(
                "AND g.measure_key=?" if measure_key else "",
                "AND g.plan_key=?" if plan_key else ""
            ), [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['channel_consent'] = dict(ch_rows) if ch_rows else {}
        except Exception:
            result['channel_consent'] = {}

        # Propensity distribution for open gaps
        try:
            prop_rows = conn.execute("""
                SELECT
                    MIN(nba_propensity_score) AS min,
                    MAX(nba_propensity_score) AS max,
                    ROUND(AVG(nba_propensity_score), 3) AS mean
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND (source_id=? OR source_id IS NULL)
                  {}{}
            """.format(
                "AND measure_key=?" if measure_key else "",
                "AND plan_key=?" if plan_key else ""
            ), [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()

            # Percentiles via ordered query
            all_props = conn.execute("""
                SELECT nba_propensity_score FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND (source_id=? OR source_id IS NULL)
                  {}{}
                ORDER BY nba_propensity_score
            """.format(
                "AND measure_key=?" if measure_key else "",
                "AND plan_key=?" if plan_key else ""
            ), [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchall()

            scores = [r[0] for r in all_props if r[0] is not None]
            n = len(scores)
            def pct(p):
                if not scores: return None
                idx = int(n * p / 100)
                return round(scores[min(idx, n-1)], 3)

            pd_dist = dict(prop_rows) if prop_rows else {}
            pd_dist['p25'] = pct(25)
            pd_dist['p50'] = pct(50)
            pd_dist['p75'] = pct(75)
            pd_dist['n'] = n
            result['propensity_distribution'] = pd_dist
        except Exception:
            result['propensity_distribution'] = {}

        # Historical performance
        try:
            hist_q = """
                SELECT COUNT(*) AS total_outreached,
                       SUM(CASE WHEN LOWER(g.gap_status)='closed' THEN 1 ELSE 0 END) AS closed_count
                FROM fact_nba_outreach_plan o
                JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
                WHERE o.status IN ('COMPLETED','SENT','SCHEDULED')
            """
            h_params = []
            if measure_key:
                hist_q += " AND g.measure_key=?"
                h_params.append(measure_key)
            if plan_key:
                hist_q += " AND g.plan_key=?"
                h_params.append(plan_key)
            h_row = conn.execute(hist_q, h_params).fetchone()
            if h_row and h_row['total_outreached']:
                hd = dict(h_row)
                hd['pct_closed'] = round(hd['closed_count'] / max(hd['total_outreached'], 1), 3)
                result['historical_performance'] = hd
            else:
                result['historical_performance'] = {'total_outreached': 0, 'closed_count': 0, 'pct_closed': None}
        except Exception:
            result['historical_performance'] = {}

        # Avg days open
        try:
            avg_q = """
                SELECT ROUND(AVG(days_open), 1) AS avg_days_open FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND (source_id=? OR source_id IS NULL)
                  {}{}
            """.format(
                "AND measure_key=?" if measure_key else "",
                "AND plan_key=?" if plan_key else ""
            )
            avg_row = conn.execute(avg_q, [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['avg_days_open'] = avg_row[0] if avg_row else None
        except Exception:
            result['avg_days_open'] = None

        # Prior year gap pct
        try:
            py_q = """
                SELECT ROUND(AVG(CASE WHEN LOWER(previous_year_gap_flag)='true' THEN 1.0 ELSE 0.0 END), 3) AS prior_year_gap_pct
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND (source_id=? OR source_id IS NULL)
                  {}{}
            """.format(
                "AND measure_key=?" if measure_key else "",
                "AND plan_key=?" if plan_key else ""
            )
            py_row = conn.execute(py_q, [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['prior_year_gap_pct'] = py_row[0] if py_row else None
        except Exception:
            result['prior_year_gap_pct'] = None

        # Avg clinical risk
        try:
            cr_q = """
                SELECT ROUND(AVG(clinical_risk_score), 3) AS avg_clinical_risk
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND (source_id=? OR source_id IS NULL)
                  {}{}
            """.format(
                "AND measure_key=?" if measure_key else "",
                "AND plan_key=?" if plan_key else ""
            )
            cr_row = conn.execute(cr_q, [source_id] + ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['avg_clinical_risk'] = cr_row[0] if cr_row else None
        except Exception:
            result['avg_clinical_risk'] = None

    finally:
        conn.close()

    return result


def get_national_benchmarks(measure_key):
    """Query measure_benchmarks table for national comparison data."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT national_avg_rate, top_quartile_rate, bottom_quartile_rate
               FROM measure_benchmarks WHERE measure_key=?
               ORDER BY benchmark_year DESC LIMIT 1""",
            (measure_key,)
        ).fetchone()
        if not row:
            return {"note": "not in table", "measure_key": measure_key}
        return dict(row)
    finally:
        conn.close()


def get_plan_population(plan_key):
    """Query plan_population table for true member counts and revenue."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT total_members, plan_revenue FROM plan_population WHERE plan_key=? LIMIT 1",
            (plan_key,)
        ).fetchone()
        if not row:
            return {"note": "not in table"}
        return dict(row)
    finally:
        conn.close()


def get_intervention_complexity(measure_key, plan_key=None, source_id='demo'):
    """Assess how hard this gap is to close based on data signals."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    result = {}
    try:
        q = """
            SELECT
                ROUND(AVG(days_open), 1) AS avg_days_open,
                ROUND(AVG(CASE WHEN LOWER(previous_year_gap_flag)='true' THEN 1.0 ELSE 0.0 END), 3) AS prior_year_gap_pct,
                ROUND(AVG(clinical_risk_score), 3) AS avg_clinical_risk,
                COUNT(*) AS total_open
            FROM fact_member_gap
            WHERE LOWER(gap_status) IN ('open','borderline','partial')
              AND (source_id=? OR source_id IS NULL)
              AND measure_key=?
        """
        params = [source_id, measure_key]
        if plan_key:
            q += " AND plan_key=?"
            params.append(plan_key)
        row = conn.execute(q, params).fetchone()
        result = dict(row) if row else {}

        # Channel distribution
        ch_q = """
            SELECT upstream_recommended_channel, COUNT(*) AS cnt
            FROM fact_member_gap
            WHERE LOWER(gap_status) IN ('open','borderline','partial')
              AND (source_id=? OR source_id IS NULL)
              AND measure_key=?
        """
        ch_params = [source_id, measure_key]
        if plan_key:
            ch_q += " AND plan_key=?"
            ch_params.append(plan_key)
        ch_q += " GROUP BY upstream_recommended_channel"
        ch_rows = conn.execute(ch_q, ch_params).fetchall()
        result['channel_distribution'] = {r['upstream_recommended_channel']: r['cnt'] for r in ch_rows}

    finally:
        conn.close()
    return result


def write_financial_analysis(analysis_obj):
    """Upsert Claude's financial analysis into the financial_analyses table."""
    measure_key = analysis_obj.get('measure_key', '')
    plan_key = analysis_obj.get('plan_key', '')
    analysis_id = f"FA_{measure_key}_{plan_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    record = {
        'analysis_id': analysis_id,
        'measure_key': measure_key,
        'plan_key': plan_key,
        'source_id': analysis_obj.get('source_id', 'demo'),
        'analysis_version': analysis_obj.get('analysis_version', '1.0'),
        'eligible_population': analysis_obj.get('eligible_population'),
        'current_compliance_rate': analysis_obj.get('current_compliance_rate'),
        'national_benchmark': analysis_obj.get('national_benchmark'),
        'gap_to_benchmark': analysis_obj.get('gap_to_benchmark'),
        'open_gaps_count': analysis_obj.get('open_gaps_count'),
        'tier_1_count': analysis_obj.get('tier_1_count'),
        'tier_1_definition': analysis_obj.get('tier_1_definition'),
        'tier_1_closure_rate': analysis_obj.get('tier_1_closure_rate'),
        'tier_1_closure_rationale': analysis_obj.get('tier_1_closure_rationale'),
        'tier_1_cost_per_member': analysis_obj.get('tier_1_cost_per_member'),
        'tier_1_cost_rationale': analysis_obj.get('tier_1_cost_rationale'),
        'tier_1_expected_closures': analysis_obj.get('tier_1_expected_closures'),
        'tier_2_count': analysis_obj.get('tier_2_count'),
        'tier_2_definition': analysis_obj.get('tier_2_definition'),
        'tier_2_closure_rate': analysis_obj.get('tier_2_closure_rate'),
        'tier_2_closure_rationale': analysis_obj.get('tier_2_closure_rationale'),
        'tier_2_cost_per_member': analysis_obj.get('tier_2_cost_per_member'),
        'tier_2_expected_closures': analysis_obj.get('tier_2_expected_closures'),
        'tier_3_count': analysis_obj.get('tier_3_count'),
        'tier_3_definition': analysis_obj.get('tier_3_definition'),
        'tier_3_closure_rate': analysis_obj.get('tier_3_closure_rate'),
        'tier_3_closure_rationale': analysis_obj.get('tier_3_closure_rationale'),
        'tier_3_cost_per_member': analysis_obj.get('tier_3_cost_per_member'),
        'tier_3_expected_closures': analysis_obj.get('tier_3_expected_closures'),
        'expected_total_closures': analysis_obj.get('expected_total_closures'),
        'stars_improvement': analysis_obj.get('stars_improvement'),
        'stars_improvement_rationale': analysis_obj.get('stars_improvement_rationale'),
        'cms_bonus_impact': analysis_obj.get('cms_bonus_impact'),
        'total_outreach_cost': analysis_obj.get('total_outreach_cost'),
        'net_return': analysis_obj.get('net_return'),
        'return_per_dollar': analysis_obj.get('return_per_dollar'),
        'confidence_level': analysis_obj.get('confidence_level'),
        'confidence_rationale': analysis_obj.get('confidence_rationale'),
        'key_risks': json.dumps(analysis_obj.get('key_risks', [])) if isinstance(analysis_obj.get('key_risks'), list) else analysis_obj.get('key_risks', ''),
        'key_opportunities': json.dumps(analysis_obj.get('key_opportunities', [])) if isinstance(analysis_obj.get('key_opportunities'), list) else analysis_obj.get('key_opportunities', ''),
        'recommended_approach': analysis_obj.get('recommended_approach'),
        'plain_english_summary': analysis_obj.get('plain_english_summary'),
        'created_timestamp': datetime.now().isoformat(),
        'claude_model_used': MODEL,
    }

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ensure_financial_analyses_table(conn)
        cols = list(record.keys())
        placeholders = ','.join(['?' for _ in cols])
        conn.execute(
            f"INSERT OR REPLACE INTO financial_analyses ({','.join(cols)}) VALUES ({placeholders})",
            [record[c] for c in cols]
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM financial_analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        return dict(saved) if saved else record
    finally:
        conn.close()


# ── Tools list for OpenAI function-calling ─────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_measure_data",
            "description": "Retrieve all data for a measure × plan combination: gap distribution, member profiles (digital literacy, language, SES), channel consent rates, propensity distribution, historical outreach performance, avg days open, prior year gap rate, and avg clinical risk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measure_key": {"type": "string", "description": "The measure key (e.g. M001)"},
                    "plan_key": {"type": "string", "description": "The plan key (e.g. P001)"},
                    "source_id": {"type": "string", "description": "Data source ID (default: demo)"}
                },
                "required": ["measure_key", "plan_key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_national_benchmarks",
            "description": "Get NCQA HEDIS national benchmark rates for a measure: national average, top quartile, bottom quartile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measure_key": {"type": "string", "description": "The measure key (e.g. M001)"}
                },
                "required": ["measure_key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan_population",
            "description": "Get accurate total member count and annual revenue for a plan from the plan_population table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_key": {"type": "string", "description": "The plan key (e.g. P001)"}
                },
                "required": ["plan_key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_intervention_complexity",
            "description": "Assess how difficult this gap is to close: avg days open, prior year gap rate, avg clinical risk, and recommended channel distribution for open-gap members.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measure_key": {"type": "string", "description": "The measure key (e.g. M001)"},
                    "plan_key": {"type": "string", "description": "Optional plan key to narrow scope"},
                    "source_id": {"type": "string", "description": "Data source ID (default: demo)"}
                },
                "required": ["measure_key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_financial_analysis",
            "description": "Write the complete financial analysis to the database. Call this only once, after gathering all data and completing your analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measure_key": {"type": "string"},
                    "plan_key": {"type": "string"},
                    "source_id": {"type": "string"},
                    "analysis_version": {"type": "string"},
                    "eligible_population": {"type": "integer"},
                    "current_compliance_rate": {"type": "number"},
                    "national_benchmark": {"type": "number"},
                    "gap_to_benchmark": {"type": "number"},
                    "open_gaps_count": {"type": "integer"},
                    "tier_1_count": {"type": "integer"},
                    "tier_1_definition": {"type": "string", "description": "Plain English definition of Tier 1 based on actual data patterns observed"},
                    "tier_1_closure_rate": {"type": "number"},
                    "tier_1_closure_rationale": {"type": "string", "description": "Specific rationale citing actual data numbers"},
                    "tier_1_cost_per_member": {"type": "number"},
                    "tier_1_cost_rationale": {"type": "string"},
                    "tier_1_expected_closures": {"type": "integer"},
                    "tier_2_count": {"type": "integer"},
                    "tier_2_definition": {"type": "string"},
                    "tier_2_closure_rate": {"type": "number"},
                    "tier_2_closure_rationale": {"type": "string"},
                    "tier_2_cost_per_member": {"type": "number"},
                    "tier_2_expected_closures": {"type": "integer"},
                    "tier_3_count": {"type": "integer"},
                    "tier_3_definition": {"type": "string"},
                    "tier_3_closure_rate": {"type": "number"},
                    "tier_3_closure_rationale": {"type": "string"},
                    "tier_3_cost_per_member": {"type": "number"},
                    "tier_3_expected_closures": {"type": "integer"},
                    "expected_total_closures": {"type": "integer"},
                    "stars_improvement": {"type": "number"},
                    "stars_improvement_rationale": {"type": "string"},
                    "cms_bonus_impact": {"type": "number"},
                    "total_outreach_cost": {"type": "number"},
                    "net_return": {"type": "number"},
                    "return_per_dollar": {"type": "number"},
                    "confidence_level": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "confidence_rationale": {"type": "string"},
                    "key_risks": {"type": "array", "items": {"type": "string"}},
                    "key_opportunities": {"type": "array", "items": {"type": "string"}},
                    "recommended_approach": {"type": "string"},
                    "plain_english_summary": {"type": "string", "description": "3-4 sentences a VP of Quality at Aetna would understand. Specific numbers."}
                },
                "required": ["measure_key", "plan_key", "tier_1_definition", "tier_1_closure_rationale", "plain_english_summary"]
            }
        }
    }
]


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def _dispatch_tool(name, args, source_id='demo'):
    if name == 'get_measure_data':
        return get_measure_data(args.get('measure_key'), args.get('plan_key'), source_id)
    elif name == 'get_national_benchmarks':
        return get_national_benchmarks(args['measure_key'])
    elif name == 'get_plan_population':
        return get_plan_population(args['plan_key'])
    elif name == 'get_intervention_complexity':
        return get_intervention_complexity(args['measure_key'], args.get('plan_key'), source_id)
    elif name == 'write_financial_analysis':
        return write_financial_analysis(args)
    else:
        return {"error": f"Unknown tool: {name}"}


# ── Main analysis function ─────────────────────────────────────────────────────

def analyze_opportunity(measure_key: str, plan_key: str, source_id: str = 'demo') -> dict:
    """Run the agentic financial analysis loop for one measure × plan combination."""

    system_prompt = """You are a healthcare analytics expert specializing in Medicare Advantage Stars improvement programs.
You have deep knowledge of HEDIS measures, member outreach effectiveness, and CMS payment structures.

Your job is to analyze a specific care gap opportunity and produce realistic, defensible financial
projections that a VP of Quality at a health plan would trust.

CRITICAL RULES:
- Never guess or fabricate data. Use only what the tools return.
- Base closure rates on the actual member profiles you see in the data — digital literacy, language,
  age, propensity scores, channel consent.
- Consider intervention complexity — a medication refill is not the same as a colonoscopy.
- If historical outreach data exists for this measure and plan, weight it heavily over generic benchmarks.
- Explain your reasoning for every number you produce — rationale fields must be specific not generic.
- Return realistic numbers. A plan with mostly low-literacy elderly Spanish-speaking members will have
  different closure rates than a plan with young digitally-engaged members even for the same measure.
- CRITICAL — eligible_population for Stars: use plan_population.total_members × eligibility_rate
  for the measure. Eligibility rates: BCS=28%, COL=42%, EED=12%, CDC=32%, MAD=12%, AFV=75%, SPC=18%.
  Do NOT use the count of members with gaps in the database as the eligible_population.
  Example: if plan has 18,000 members and measure is MAD (12%), eligible_population = 18,000 × 0.12 = 2,160.
- Stars improvement formula: (expected_closures / eligible_population) × star_weight × 0.5
  Cap at star_weight × 0.10 per campaign.
- CMS bonus: stars_improvement × plan_revenue × 0.05
- Cost per member must reflect actual channel costs:
  Email: $1.50, SMS: $0.50 + incentive cost, Call: $8.00 + incentive cost.
  Incentive costs: GIFTCARD_15=$15, GIFTCARD_25=$25, TRANSPORT_VOUCHER=$20, FIT_KIT_MAILER=$8.
- Confidence level based on data quality: eligible population size, whether historical data exists.
- Define tiers based on what you actually observe in the data — propensity distribution, channel
  consent, digital literacy. Do not use generic 0.70/0.45 thresholds unless the data supports them.
- plain_english_summary: 2-3 sentences max. Specific numbers only.
- All rationale fields: 1-2 sentences max. Be specific but brief — cite the key number and why.
- key_risks and key_opportunities: 2-3 items each, one sentence per item."""

    user_message = f"""Analyze the gap closure opportunity for measure {measure_key} on plan {plan_key}.

Step 1: Call get_measure_data to understand the population and gap distribution.
Step 2: Call get_national_benchmarks to understand where this plan stands nationally.
Step 3: Call get_plan_population to get accurate revenue and member counts.
Step 4: Call get_intervention_complexity to understand how hard this gap is to close.
Step 5: Based on everything you've seen, call write_financial_analysis with your complete analysis.

For tier definitions: look at the actual propensity distribution, digital literacy, language, and
channel consent data you retrieved. Define tiers based on natural breakpoints in the data.

For closure rates: reason from the data. Consider intervention complexity, member profile, channel
availability, prior year gap rate. If historical data exists, use those actual rates.

In tier_X_closure_rationale: be specific — cite actual numbers from the data you saw.
In plain_english_summary: be specific with numbers. Do not use generic language."""

    # Ensure table exists
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_financial_analyses_table(conn)
    conn.commit()
    conn.close()

    messages = [{"role": "user", "content": user_message}]

    for iteration in range(10):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=8000,
        )

        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in (msg.tool_calls or [])
            ] if msg.tool_calls else None
        })

        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            raw = tc.function.arguments
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                # Response was truncated — strip trailing incomplete content and retry
                truncated = raw.rstrip()
                # Remove trailing incomplete key-value pairs
                for end_char in ['}', ']']:
                    last = truncated.rfind(end_char)
                    if last != -1:
                        candidate = truncated[:last+1]
                        try:
                            args = json.loads(candidate)
                            break
                        except json.JSONDecodeError:
                            continue
                else:
                    logging.error(f"[financial] Could not parse tool args for {tc.function.name}")
                    continue
            result = _dispatch_tool(tc.function.name, args, source_id)
            if tc.function.name == 'write_financial_analysis':
                logging.info(f"[financial] Analysis written for {measure_key} x {plan_key}")
                return result  # Done — return the written analysis
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str)
            })

    return {"error": "Analysis did not complete"}


# ── Batch analysis ─────────────────────────────────────────────────────────────

def analyze_all_opportunities(source_id: str = 'demo') -> list:
    """Run financial analysis for all open measure × plan combinations."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    pairs = conn.execute(
        "SELECT DISTINCT measure_key, plan_key FROM fact_member_gap WHERE (source_id=? OR source_id IS NULL) AND LOWER(gap_status) IN ('open','borderline','partial')",
        (source_id,)
    ).fetchall()
    conn.close()

    results = []
    for row in pairs:
        try:
            result = analyze_opportunity(row['measure_key'], row['plan_key'], source_id)
            results.append(result)
            logging.info(f"[financial] Analyzed {row['measure_key']} x {row['plan_key']}")
        except Exception as e:
            logging.error(f"[financial] Error analyzing {row['measure_key']} x {row['plan_key']}: {e}")
    return results
