"""
CareIntel Financial Analysis Loop — agentic analysis using the Anthropic SDK directly.

Data-reading functions (get_measure_data, get_intervention_complexity) route through
db_adapter so they query whichever source is active (SQLite or Snowflake).
Write functions and benchmark lookups always use the local SQLite file.
"""

import anthropic as _anthropic_sdk
from dotenv import load_dotenv
import sqlite3
import json
import os
import logging
from datetime import datetime

load_dotenv()

# ── db_adapter helpers ─────────────────────────────────────────────────────────
def _active_conn():
    """Return a connection to the currently active data source (SQLite or Snowflake)."""
    try:
        from db_adapter import get_db_connection
        return get_db_connection()
    except Exception:
        return _sqlite_conn()

def _sqlite_conn():
    """Always returns a local SQLite connection (for metadata/cache tables)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _is_sf():
    """True when the active backend is Snowflake."""
    try:
        from db_adapter import get_current_mode
        return get_current_mode() == 'snowflake'
    except Exception:
        return False

def _src_filter(source_id: str) -> str:
    """Return a WHERE fragment for source_id partitioning.
    In Snowflake mode all rows belong to one schema — no filter needed."""
    if _is_sf():
        return "1=1"
    return f"(source_id='{source_id}' OR source_id IS NULL)"

_API_KEY = (
    os.getenv('ANTHROPIC_API_KEY') or
    os.getenv('OPENROUTER_API_KEY') or
    os.getenv('OPENAI_API_KEY') or
    ''
)
if not _API_KEY:
    logging.warning("[financial] No API key found. Set ANTHROPIC_API_KEY in .env")

# Auto-detect provider: OpenRouter keys start with sk-or-; Anthropic keys start with sk-ant-
_IS_OPENROUTER = _API_KEY.startswith("sk-or-") or bool(os.getenv('OPENROUTER_API_KEY'))
_IS_ANTHROPIC  = _API_KEY.startswith("sk-ant-") or (not _IS_OPENROUTER and _API_KEY.startswith("sk-"))

if _IS_OPENROUTER:
    from openai import OpenAI as _OpenAI
    client = _OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_API_KEY or 'missing-key',
        default_headers={"HTTP-Referer": "https://careintel.exl.com", "X-Title": "CareIntel Financial Analysis"}
    )
    _PROVIDER = "openrouter"
    MODEL = "anthropic/claude-opus-4-8"
else:
    client = _anthropic_sdk.Anthropic(api_key=_API_KEY or 'missing-key')
    _PROVIDER = "anthropic"
    MODEL = "claude-opus-4-8"

logging.info(f"[financial] Using provider={_PROVIDER} model={MODEL}")

DB_PATH = os.getenv('DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'careintel.db'))


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
    """Pull all relevant data for a measure × plan combination.
    Queries the active data source (SQLite or Snowflake)."""
    conn = _active_conn()
    result = {}

    try:
        # Measure metadata
        if measure_key:
            try:
                m_row = conn.execute(
                    "SELECT measure_key, measure_code, measure_name, star_weight, clinical_description FROM dim_measure WHERE measure_key=?",
                    (measure_key,)
                ).fetchone()
            except Exception:
                m_row = conn.execute(
                    "SELECT measure_key, measure_code, measure_name, star_weight FROM dim_measure WHERE measure_key=?",
                    (measure_key,)
                ).fetchone()
            if m_row:
                m_dict = dict(m_row)
                m_dict.setdefault('clinical_description', '')
                result['measure'] = m_dict
            else:
                result['measure'] = {}
        else:
            result['measure'] = {}

        # Plan metadata — try with plan_population join (SQLite), fall back to dim_plan_contract alone (Snowflake)
        if plan_key:
            try:
                p_row = conn.execute(
                    """SELECT p.plan_key, p.plan_name, p.plan_annual_revenue, p.total_members,
                              p.star_rating_current, p.star_rating_target, p.region, p.segment,
                              pp.total_members AS pop_members, pp.plan_revenue AS pop_revenue
                       FROM dim_plan_contract p
                       LEFT JOIN plan_population pp ON pp.plan_key = p.plan_key
                       WHERE p.plan_key=?""",
                    (plan_key,)
                ).fetchone()
            except Exception:
                # plan_population table doesn't exist in Snowflake — plain join
                p_row = conn.execute(
                    "SELECT plan_key, plan_name, plan_annual_revenue, total_members, star_rating_current, star_rating_target, region, segment FROM dim_plan_contract WHERE plan_key=?",
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

        # Gap distribution — use _src_filter() so Snowflake skips source_id partitioning
        _sf = _src_filter(source_id)
        gap_q = f"""
            SELECT
                COUNT(*) AS total_gaps,
                SUM(CASE WHEN LOWER(gap_status) IN ('open','borderline','partial') THEN 1 ELSE 0 END) AS open_gaps,
                SUM(CASE WHEN LOWER(gap_status) = 'closed' THEN 1 ELSE 0 END) AS closed_gaps,
                SUM(CASE WHEN LOWER(gap_status) = 'borderline' THEN 1 ELSE 0 END) AS borderline,
                SUM(CASE WHEN LOWER(gap_status) = 'partial' THEN 1 ELSE 0 END) AS partial
            FROM fact_member_gap
            WHERE {_sf}
        """
        params = []
        if measure_key:
            gap_q += " AND measure_key=?"
            params.append(measure_key)
        if plan_key:
            gap_q += " AND plan_key=?"
            params.append(plan_key)
        gd = conn.execute(gap_q, params).fetchone()
        result['gap_distribution'] = dict(gd) if gd else {}

        # Member profiles (digital_literacy, language, ses) for open gaps
        _gsf = _src_filter(source_id).replace('source_id', 'g.source_id')
        try:
            mp_rows = conn.execute(f"""
                SELECT m.digital_literacy_segment, m.language_preference, m.socioeconomic_segment, COUNT(*) AS cnt
                FROM fact_member_gap g
                JOIN dim_member m ON m.member_key = g.member_key
                WHERE LOWER(g.gap_status) IN ('open','borderline','partial')
                  AND {_gsf}
                  {"AND g.measure_key=?" if measure_key else ""}
                  {"AND g.plan_key=?" if plan_key else ""}
                GROUP BY m.digital_literacy_segment, m.language_preference, m.socioeconomic_segment
            """, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchall()

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
        except Exception as _e:
            logging.debug(f"[financial] member_profiles failed: {_e}")
            result['member_profiles'] = {}

        # Channel consent
        try:
            ch_rows = conn.execute(f"""
                SELECT
                    ROUND(AVG(CASE WHEN LOWER(cp.email_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_email_allowed,
                    ROUND(AVG(CASE WHEN LOWER(cp.sms_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_sms_allowed,
                    ROUND(AVG(CASE WHEN LOWER(cp.call_allowed)='true' THEN 1.0 ELSE 0.0 END), 3) AS pct_call_allowed
                FROM fact_member_gap g
                JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
                WHERE LOWER(g.gap_status) IN ('open','borderline','partial')
                  AND {_gsf}
                  {"AND g.measure_key=?" if measure_key else ""}
                  {"AND g.plan_key=?" if plan_key else ""}
            """, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['channel_consent'] = dict(ch_rows) if ch_rows else {}
        except Exception as _e:
            logging.debug(f"[financial] channel_consent failed: {_e}")
            result['channel_consent'] = {}

        # Propensity distribution for open gaps
        _sf2 = _src_filter(source_id)
        try:
            prop_rows = conn.execute(f"""
                SELECT
                    MIN(nba_propensity_score) AS min,
                    MAX(nba_propensity_score) AS max,
                    ROUND(AVG(nba_propensity_score), 3) AS mean,
                    ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY nba_propensity_score), 3) AS p25,
                    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY nba_propensity_score), 3) AS p50,
                    ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY nba_propensity_score), 3) AS p75,
                    COUNT(*) AS n
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND {_sf2}
                  {"AND measure_key=?" if measure_key else ""}
                  {"AND plan_key=?" if plan_key else ""}
            """, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['propensity_distribution'] = dict(prop_rows) if prop_rows else {}
        except Exception:
            # Fallback: pull scores list for percentile calculation (SQLite doesn't have PERCENTILE_CONT)
            try:
                prop_stats = conn.execute(f"""
                    SELECT MIN(nba_propensity_score) AS min, MAX(nba_propensity_score) AS max,
                           ROUND(AVG(nba_propensity_score), 3) AS mean
                    FROM fact_member_gap
                    WHERE LOWER(gap_status) IN ('open','borderline','partial')
                      AND {_sf2}
                      {"AND measure_key=?" if measure_key else ""}
                      {"AND plan_key=?" if plan_key else ""}
                """, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
                all_props = conn.execute(f"""
                    SELECT nba_propensity_score FROM fact_member_gap
                    WHERE LOWER(gap_status) IN ('open','borderline','partial')
                      AND {_sf2}
                      {"AND measure_key=?" if measure_key else ""}
                      {"AND plan_key=?" if plan_key else ""}
                    ORDER BY nba_propensity_score
                """, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchall()
                scores = [r[0] for r in all_props if r[0] is not None]
                nn = len(scores)
                def _pct(p):
                    if not scores: return None
                    idx = int(nn * p / 100)
                    return round(scores[min(idx, nn-1)], 3)
                pd_dist = dict(prop_stats) if prop_stats else {}
                pd_dist.update({'p25': _pct(25), 'p50': _pct(50), 'p75': _pct(75), 'n': nn})
                result['propensity_distribution'] = pd_dist
            except Exception as _e2:
                logging.debug(f"[financial] propensity fallback failed: {_e2}")
                result['propensity_distribution'] = {}

        # Historical performance (outreach join — may not exist in Snowflake, graceful skip)
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
            result['historical_performance'] = {'total_outreached': 0, 'closed_count': 0, 'pct_closed': None}

        # Avg days open
        try:
            avg_q = f"""
                SELECT ROUND(AVG(days_open), 1) AS avg_days_open FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND {_sf2}
                  {"AND measure_key=?" if measure_key else ""}
                  {"AND plan_key=?" if plan_key else ""}
            """
            avg_row = conn.execute(avg_q, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['avg_days_open'] = avg_row[0] if avg_row else None
        except Exception:
            result['avg_days_open'] = None

        # Prior year gap pct
        try:
            py_q = f"""
                SELECT ROUND(AVG(CASE WHEN previous_year_gap_flag=1 OR LOWER(previous_year_gap_flag)='true' THEN 1.0 ELSE 0.0 END), 3) AS prior_year_gap_pct
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND {_sf2}
                  {"AND measure_key=?" if measure_key else ""}
                  {"AND plan_key=?" if plan_key else ""}
            """
            py_row = conn.execute(py_q, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
            result['prior_year_gap_pct'] = py_row[0] if py_row else None
        except Exception:
            result['prior_year_gap_pct'] = None

        # Avg clinical risk
        try:
            cr_q = f"""
                SELECT ROUND(AVG(clinical_risk_score), 3) AS avg_clinical_risk
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND {_sf2}
                  {"AND measure_key=?" if measure_key else ""}
                  {"AND plan_key=?" if plan_key else ""}
            """
            cr_row = conn.execute(cr_q, ([measure_key] if measure_key else []) + ([plan_key] if plan_key else [])).fetchone()
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
    """Query plan_population table (SQLite local) or dim_plan_contract (Snowflake) for member counts."""
    # Try local SQLite plan_population first
    local = _sqlite_conn()
    try:
        row = local.execute(
            "SELECT total_members, plan_revenue FROM plan_population WHERE plan_key=? LIMIT 1",
            (plan_key,)
        ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    finally:
        local.close()
    # Fallback: read from active source's dim_plan_contract
    conn = _active_conn()
    try:
        row = conn.execute(
            "SELECT total_members, plan_annual_revenue AS plan_revenue FROM dim_plan_contract WHERE plan_key=? LIMIT 1",
            (plan_key,)
        ).fetchone()
        return dict(row) if row else {"note": "not in table"}
    except Exception:
        return {"note": "not in table"}
    finally:
        conn.close()


def get_intervention_complexity(measure_key, plan_key=None, source_id='demo'):
    """Assess how hard this gap is to close based on data signals.
    Queries the active data source (SQLite or Snowflake)."""
    conn = _active_conn()
    _sf3 = _src_filter(source_id)
    result = {}
    try:
        q = f"""
            SELECT
                ROUND(AVG(days_open), 1) AS avg_days_open,
                ROUND(AVG(CASE WHEN previous_year_gap_flag=1 OR LOWER(previous_year_gap_flag)='true' THEN 1.0 ELSE 0.0 END), 3) AS prior_year_gap_pct,
                ROUND(AVG(clinical_risk_score), 3) AS avg_clinical_risk,
                COUNT(*) AS total_open
            FROM fact_member_gap
            WHERE LOWER(gap_status) IN ('open','borderline','partial')
              AND {_sf3}
              AND measure_key=?
        """
        params = [measure_key]
        if plan_key:
            q += " AND plan_key=?"
            params.append(plan_key)
        row = conn.execute(q, params).fetchone()
        result = dict(row) if row else {}

        # Channel distribution
        try:
            ch_q = f"""
                SELECT upstream_recommended_channel, COUNT(*) AS cnt
                FROM fact_member_gap
                WHERE LOWER(gap_status) IN ('open','borderline','partial')
                  AND {_sf3}
                  AND measure_key=?
            """
            ch_params = [measure_key]
            if plan_key:
                ch_q += " AND plan_key=?"
                ch_params.append(plan_key)
            ch_q += " GROUP BY upstream_recommended_channel"
            ch_rows = conn.execute(ch_q, ch_params).fetchall()
            result['channel_distribution'] = {r['upstream_recommended_channel']: r['cnt'] for r in ch_rows}
        except Exception:
            result['channel_distribution'] = {}

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


# ── Tools list ────────────────────────────────────────────────────────────────
# Anthropic native format; converted to OpenAI/OpenRouter format below.

TOOLS = [
    {
        "name": "get_measure_data",
        "description": "Retrieve all data for a measure × plan combination: gap distribution, member profiles, channel consent rates, propensity distribution, historical outreach performance, avg days open, prior year gap rate, and avg clinical risk.",
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string", "description": "The measure key (e.g. M001 or MX001)"},
                "plan_key": {"type": "string", "description": "The plan key (e.g. P001 or MP001)"},
                "source_id": {"type": "string", "description": "Data source ID (default: demo)"}
            },
            "required": ["measure_key", "plan_key"]
        }
    },
    {
        "name": "get_national_benchmarks",
        "description": "Get NCQA HEDIS national benchmark rates for a measure: national average, top quartile, bottom quartile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string"}
            },
            "required": ["measure_key"]
        }
    },
    {
        "name": "get_plan_population",
        "description": "Get accurate total member count and annual revenue for a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_key": {"type": "string"}
            },
            "required": ["plan_key"]
        }
    },
    {
        "name": "get_intervention_complexity",
        "description": "Assess how difficult this gap is to close: avg days open, prior year gap rate, avg clinical risk, and channel distribution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "measure_key": {"type": "string"},
                "plan_key": {"type": "string"},
                "source_id": {"type": "string"}
            },
            "required": ["measure_key"]
        }
    },
    {
        "name": "write_financial_analysis",
        "description": "Write the complete financial analysis to the database. Call this only once, after all data is gathered.",
        "input_schema": {
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
                "tier_1_definition": {"type": "string"},
                "tier_1_closure_rate": {"type": "number"},
                "tier_1_closure_rationale": {"type": "string"},
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
                "plain_english_summary": {"type": "string", "description": "5 pipe-separated sections: FORMULA | POPULATION | COMPLETIONS | PLAN FINANCIALS | PATIENT BENEFITS with real numbers."}
            },
            "required": ["measure_key", "plan_key", "tier_1_definition", "tier_1_closure_rationale", "plain_english_summary"]
        }
    }
]

# Convert to OpenAI/OpenRouter format for that provider path
_TOOLS_OR = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        }
    }
    for t in TOOLS
]

def _active_tools():
    """Return tools in the format expected by the current provider."""
    return _TOOLS_OR if _IS_OPENROUTER else TOOLS


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

════════════════════════════════════════════════════════════════
CLOSURE RATE BENCHMARKS — use as anchors, adjust based on data
════════════════════════════════════════════════════════════════
Industry-standard Medicare Advantage HEDIS outreach closure rates:

• Medication adherence (MAD, CBP, CDC refills):
    T1 (high propensity, digital): 25–40%
    T2 (mid propensity, SMS+incentive): 15–25%
    T3 (low propensity, call+voucher): 8–15%

• Preventive screening (BCS mammogram, EED eye exam, AFV annual visit):
    T1: 15–28%    T2: 8–16%    T3: 4–10%

• Procedure-based screening (COL colonoscopy / FIT kit, SPC):
    T1: 8–18%    T2: 4–12%    T3: 2–6%

Adjustments (cumulative, apply all that fit):
  + High digital literacy + email/SMS consent ≥ 70%: +5 to +10 pp on T1
  − Non-English preferred / low digital literacy > 30% of members: −5 to −10 pp on T1/T2
  − Prior year gap (chronic non-complier) > 50%: −5 to −8 pp all tiers
  − Avg days open > 200: −3 to −5 pp (late closers are harder to motivate)
  − Very high incentive complexity (colonoscopy requires procedure, not self-administered): already in range above

HISTORICAL DATA WEIGHTING RULE:
  n ≥ 100 outreached: weight 60% historical rate, 40% benchmark → historical dominates
  n = 20–99 outreached: weight 40% historical, 60% benchmark → benchmark guides
  n < 20 outreached:  weight 10% historical, 90% benchmark → ignore history almost entirely
  Apply the blended rate as the T2 baseline, then set T1 = T2 × 1.5 (capped at benchmark top) and T3 = T2 × 0.55.

HARD FLOOR — NEVER go below these without n ≥ 200 historical data:
  Medication measures: T1 = 15%, T2 = 8%, T3 = 4%
  Screening measures:  T1 = 10%, T2 = 5%, T3 = 2.5%
  Procedure measures:  T1 = 6%,  T2 = 3%, T3 = 1.5%

CHANNEL CONSENT NOTE — zero email/SMS consent does NOT make closure rate zero:
  Direct mail (postcards, FIT kit mailers) is ALWAYS available. If email/SMS consent is 0%,
  the plan defaults to mail-only outreach. Mail response rates are lower than digital
  but still generate closures: mail-only adjustment = −5 to −8 pp from benchmark floor.
  Never use channel constraint to push closure rate below the hard floor above.

════════════════════════════════════════════════════════════════
CALCULATION RULES
════════════════════════════════════════════════════════════════
- eligible_population for Stars = plan_population.total_members × eligibility_rate
  Eligibility rates: BCS=28%, COL=42%, EED=12%, CDC=32%, MAD=12%, AFV=75%, SPC=18%.
  Do NOT use the gap database count — those are a small sample of the true eligible population.
- Stars improvement = (expected_closures / eligible_population) × star_weight × 0.5
  Cap per campaign at star_weight × 0.10.
- CMS bonus = stars_improvement × plan_revenue × 0.05
- Medical cost savings: Non-compliant members cost the plan more due to preventable complications.
  Closing a gap moves a member from the non-compliant to compliant bucket, saving:
    savings_per_closure = annual_PMPM × measure_savings_rate
  Measure savings rates (% of annual PMPM saved per closed gap):
    BCS=15%, COL=30%, EED=22%, CDC=18%, MAD=15%, AFV=10%, SPC=12%
  Total medical savings = expected_closures × savings_per_closure
  Total benefit = CMS bonus + medical savings (both are real plan revenue/savings)
  Net return = total_benefit − total_outreach_cost
- Cost per member: Email=$1.50, SMS=$0.50+incentive, Call=$8.00+incentive
  Incentive costs: GIFTCARD_15=$15, GIFTCARD_25=$25, TRANSPORT_VOUCHER=$20, FIT_KIT_MAILER=$8.

════════════════════════════════════════════════════════════════
TIER SIZING RULES
════════════════════════════════════════════════════════════════
Use the plan's eligible_population (total_members × elig_rate) minus already-compliant members
as the open_gaps_count. Split into tiers based on propensity/digital literacy signals in the data:
  Roughly: T1 ≈ 20–30% of open gaps (high propensity or high digital)
           T2 ≈ 40–50% (medium propensity)
           T3 ≈ 20–30% (low propensity or high barrier)
Adjust the splits based on what you actually observe in digital_literacy and channel_consent data.

════════════════════════════════════════════════════════════════
OUTPUT RULES
════════════════════════════════════════════════════════════════
- plain_english_summary: Detailed PM briefing in exactly 5 pipe-separated sections (use ' | ' as section separator):
  SECTION 1 — FORMULA: "FORMULA: CMS bonus = Stars_improvement × plan_revenue × 5% | Medical savings = closures × annual_PMPM × savings_rate ([X]% for [measure])"
  SECTION 2 — POPULATION: "POPULATION: [N] total eligible members, [M] open gaps ([K]% non-compliant). T1 (high propensity): [n1] members. T2 (mid propensity): [n2] members. T3 (low propensity): [n3] members."
  SECTION 3 — EXPECTED COMPLETIONS: "COMPLETIONS: T1 [n1] members × [r1]% = [c1] closures. T2 [n2] × [r2]% = [c2] closures. Total: [c1+c2] gap closures ([pct]% of open gaps closed)."
  SECTION 4 — PLAN FINANCIALS: "PLAN FINANCIALS: Stars impact +[x]★. CMS bonus: +[x]★ × $[rev] × 5% = $[cms]. Medical savings: [closures] × $[pmpm_annual] × [rate]% = $[med_sav]. Total plan benefit: $[cms+med]. Campaign cost: $[cost]. Net return: $[net]."
  SECTION 5 — PATIENT BENEFITS: "PATIENT BENEFITS: [1-2 specific health outcomes for members who get this gap closed, citing clinical evidence where possible — e.g. survival rates, reduction in hospitalizations, quality of life improvement]."
  Write all 5 sections as one continuous string with ' | ' between them. Use real numbers from your analysis. No hedging or filler.
- All rationale fields: 1-2 sentences max. Cite the specific number and the rule you applied.
- key_risks and key_opportunities: 2-3 items each, one sentence per item.
- Confidence: HIGH if n_members > 5,000 and historical n ≥ 50; MEDIUM otherwise; LOW if data is very sparse."""

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

    # Ensure local cache table exists
    local_conn = sqlite3.connect(DB_PATH)
    local_conn.row_factory = sqlite3.Row
    ensure_financial_analyses_table(local_conn)
    local_conn.commit()
    local_conn.close()

    messages = [{"role": "user", "content": user_message}]

    for iteration in range(10):
        wrote_analysis = False
        final_result = None

        if _IS_OPENROUTER:
            # ── OpenRouter / OpenAI SDK path ──────────────────────────────────
            or_messages = [{"role": "system", "content": system_prompt}] + messages
            response = client.chat.completions.create(
                model=MODEL,
                messages=or_messages,
                tools=_active_tools(),
                tool_choice="auto",
                max_tokens=8000,
            )
            choice = response.choices[0]
            msg = choice.message

            # Append assistant turn (OpenAI format)
            messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})

            # Check finish reason
            if choice.finish_reason != "tool_calls":
                break

            # Process tool calls
            tool_results = []
            for tc in (msg.tool_calls or []):
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                result = _dispatch_tool(tool_name, args, source_id)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })
                if tool_name == 'write_financial_analysis':
                    logging.info(f"[financial] Analysis written for {measure_key} x {plan_key}")
                    wrote_analysis = True
                    final_result = result

            messages.extend(tool_results)

        else:
            # ── Native Anthropic SDK path ─────────────────────────────────────
            response = client.messages.create(
                model=MODEL,
                system=system_prompt,
                messages=messages,
                tools=_active_tools(),
                max_tokens=8000,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_name = block.name
                args = block.input  # already parsed dict
                result = _dispatch_tool(tool_name, args, source_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
                if tool_name == 'write_financial_analysis':
                    logging.info(f"[financial] Analysis written for {measure_key} x {plan_key}")
                    wrote_analysis = True
                    final_result = result

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        if wrote_analysis:
            return final_result

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
