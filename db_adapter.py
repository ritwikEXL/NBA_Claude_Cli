"""
db_adapter.py — Database abstraction layer for CareIntel NBA platform
======================================================================
Supports two backends:
  - SQLite   (default; local dev + Render free tier)
  - Snowflake (production; set DB_MODE=snowflake in .env)

Usage:
    from db_adapter import get_db_connection, DB_MODE

    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM dim_plan_contract").fetchall()

Both adapters expose the same interface:
    conn.execute(sql, params=[])   → cursor
    conn.executemany(sql, rows)    → cursor
    conn.commit()                  → None
    conn.close()                   → None
    conn.row_factory               → set to dict-returning factory

Environment variables:
    DB_MODE              = sqlite | snowflake   (default: sqlite)
    DB_PATH              = path to .db file     (sqlite only)
    SNOWFLAKE_ACCOUNT    = <org>-<account>
    SNOWFLAKE_USER       = <username>
    SNOWFLAKE_PASSWORD   = <password>
    SNOWFLAKE_DATABASE   = CAREINTEL
    SNOWFLAKE_SCHEMA     = NBA
    SNOWFLAKE_WAREHOUSE  = COMPUTE_WH
    SNOWFLAKE_ROLE       = SYSADMIN (optional)
"""

import os
import sqlite3
import contextlib
import logging
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── Mode detection ─────────────────────────────────────────────────────────────
DB_MODE = os.getenv("DB_MODE", "sqlite").lower().strip()

# ── SQLite helpers ─────────────────────────────────────────────────────────────
_SQLITE_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db")
)


def _dict_factory(cursor, row):
    """Make sqlite3 return dicts instead of tuples."""
    fields = [d[0] for d in cursor.description]
    return dict(zip(fields, row))


class _SQLiteConn:
    """Thin wrapper around sqlite3.Connection that mimics the Snowflake adapter interface."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = _dict_factory
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ---- Core API ----
    def execute(self, sql: str, params=None):
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows):
        return self._conn.executemany(sql, rows)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            try: self._conn.rollback()
            except Exception: pass
        else:
            self._conn.commit()
        self._conn.close()


# ── Snowflake helpers ──────────────────────────────────────────────────────────

def _sf_env(key: str, required=True) -> str:
    val = os.getenv(key, "")
    if required and not val:
        raise EnvironmentError(
            f"[db_adapter] Snowflake mode requires env var {key}. "
            "Set it in .env or Render environment."
        )
    return val


class _SnowflakeCursorWrapper:
    """Wraps a Snowflake cursor to return dicts (mirroring sqlite3 DictRow)."""

    def __init__(self, cursor):
        self._cur = cursor

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _to_dict(self, row):
        if row is None:
            return None
        if self._cur.description is None:
            return row
        keys = [d[0].lower() for d in self._cur.description]
        return dict(zip(keys, row))

    def fetchone(self):
        return self._to_dict(self._cur.fetchone())

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows or self._cur.description is None:
            return rows
        keys = [d[0].lower() for d in self._cur.description]
        return [dict(zip(keys, r)) for r in rows]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size)
        if not rows or self._cur.description is None:
            return rows
        keys = [d[0].lower() for d in self._cur.description]
        return [dict(zip(keys, r)) for r in rows]

    def __iter__(self):
        for row in self._cur:
            if self._cur.description:
                keys = [d[0].lower() for d in self._cur.description]
                yield dict(zip(keys, row))
            else:
                yield row


class _SnowflakeConn:
    """Adapter wrapping snowflake.connector.SnowflakeConnection."""

    def __init__(self):
        try:
            import snowflake.connector as sf
        except ImportError:
            raise RuntimeError(
                "[db_adapter] snowflake-connector-python is not installed. "
                "Run:  pip install snowflake-connector-python"
            )

        connect_kwargs: dict[str, Any] = dict(
            account=_sf_env("SNOWFLAKE_ACCOUNT"),
            user=_sf_env("SNOWFLAKE_USER"),
            password=_sf_env("SNOWFLAKE_PASSWORD"),
            database=_sf_env("SNOWFLAKE_DATABASE", required=False) or "CAREINTEL",
            schema=_sf_env("SNOWFLAKE_SCHEMA", required=False) or "NBA",
            warehouse=_sf_env("SNOWFLAKE_WAREHOUSE", required=False) or "COMPUTE_WH",
        )
        role = _sf_env("SNOWFLAKE_ROLE", required=False)
        if role:
            connect_kwargs["role"] = role

        logger.info("[db_adapter] Connecting to Snowflake account=%s db=%s schema=%s",
                    connect_kwargs["account"], connect_kwargs["database"], connect_kwargs["schema"])
        self._conn = sf.connect(**connect_kwargs)
        self._conn.cursor().execute(
            f"USE WAREHOUSE {connect_kwargs['warehouse']}"
        )

    # ── SQL dialect translation ────────────────────────────────────────────────
    @staticmethod
    def _translate(sql: str) -> str:
        """
        Minimal SQLite→Snowflake SQL translation:
        - ?  →  %s  (positional bind variables)
        - PRAGMA … → no-op
        - strftime('%Y',…) → YEAR(…)
        - AUTOINCREMENT → AUTOINCREMENT (Snowflake uses AUTOINCREMENT natively)
        """
        if sql.strip().upper().startswith("PRAGMA"):
            return None
        # Positional placeholders
        sql = sql.replace("?", "%s")
        # Named placeholders :name → %(name)s
        import re
        sql = re.sub(r":(\w+)", r"%(\1)s", sql)
        # Common function differences
        sql = sql.replace("strftime('%Y',", "YEAR(").replace("strftime('%m',", "MONTH(")
        return sql

    def _cursor(self):
        return _SnowflakeCursorWrapper(self._conn.cursor())

    def execute(self, sql: str, params=None):
        sql_sf = self._translate(sql)
        if sql_sf is None:
            return self._cursor()  # PRAGMA no-op
        cur = self._cursor()
        if params is None:
            cur._cur.execute(sql_sf)
        else:
            cur._cur.execute(sql_sf, params)
        return cur

    def executemany(self, sql: str, rows):
        sql_sf = self._translate(sql)
        if sql_sf is None:
            return
        cur = self._cursor()
        cur._cur.executemany(sql_sf, [list(r.values()) if isinstance(r, dict) else r for r in rows])
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            try: self._conn.rollback()
            except Exception: pass
        else:
            self._conn.commit()
        self._conn.close()


# ── Public factory ─────────────────────────────────────────────────────────────

def get_db_connection():
    """
    Returns a connection object for the configured backend.
    Use as a context manager:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT …").fetchall()
    """
    if DB_MODE == "snowflake":
        logger.debug("[db_adapter] Using Snowflake backend")
        return _SnowflakeConn()
    else:
        logger.debug("[db_adapter] Using SQLite backend at %s", _SQLITE_PATH)
        return _SQLiteConn(_SQLITE_PATH)


# Alias for drop-in compatibility with existing contextlib.contextmanager usage in api.py
@contextlib.contextmanager
def get_db() -> Iterator:
    """Context manager — same name used in api.py."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        try: conn._conn.rollback()
        except Exception: pass
        raise
    finally:
        conn.close()


# ── Snowflake schema DDL ───────────────────────────────────────────────────────
SNOWFLAKE_SCHEMA_DDL = """
-- Run once in Snowflake to create the schema for CareIntel NBA
-- Adapt CAREINTEL and NBA to your own database/schema names

CREATE DATABASE IF NOT EXISTS CAREINTEL;
CREATE SCHEMA IF NOT EXISTS CAREINTEL.NBA;
USE SCHEMA CAREINTEL.NBA;

CREATE TABLE IF NOT EXISTS dim_measure (
    measure_key          VARCHAR(20)   PRIMARY KEY,
    measure_code         VARCHAR(10),
    measure_name         VARCHAR(200),
    measure_type         VARCHAR(50),
    star_weight          FLOAT,
    hedis_domain         VARCHAR(100),
    age_gender_eligibility VARCHAR(200),
    clinical_description VARCHAR(500),
    nba_default_playbook VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS dim_plan_contract (
    plan_key             VARCHAR(20)   PRIMARY KEY,
    contract_id          VARCHAR(20),
    plan_name            VARCHAR(200),
    region               VARCHAR(50),
    segment              VARCHAR(50),
    star_rating_current  FLOAT,
    star_rating_target   FLOAT,
    plan_annual_revenue  BIGINT,
    total_members        INTEGER,
    plan_pmpm_monthly    FLOAT,
    source_id            VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS dim_member (
    member_key                VARCHAR(30)  PRIMARY KEY,
    dob_year                  INTEGER,
    age_band                  VARCHAR(10),
    gender                    VARCHAR(1),
    language_preference       VARCHAR(50),
    digital_literacy_segment  VARCHAR(20),
    socioeconomic_segment     VARCHAR(20),
    pcp_provider_key          VARCHAR(20),
    display_name              VARCHAR(200),
    source_id                 VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS dim_member_channel_pref (
    member_key              VARCHAR(30)  PRIMARY KEY,
    email_allowed           VARCHAR(5),
    sms_allowed             VARCHAR(5),
    call_allowed            VARCHAR(5),
    preferred_channel       VARCHAR(20),
    do_not_contact_flag     VARCHAR(5),
    channel_risk_notes      VARCHAR(500)
);

CREATE TABLE IF NOT EXISTS fact_member_gap (
    member_gap_key                VARCHAR(60)  PRIMARY KEY,
    member_key                    VARCHAR(30),
    measure_key                   VARCHAR(20),
    measure_code                  VARCHAR(10),
    plan_key                      VARCHAR(20),
    measurement_year              INTEGER,
    gap_status                    VARCHAR(20),
    gap_open_date                 DATE,
    gap_close_date                DATE,
    days_open                     INTEGER,
    clinical_risk_score           FLOAT,
    nba_propensity_score          FLOAT,
    previous_year_gap_flag        INTEGER,
    upstream_recommended_channel  VARCHAR(20),
    upstream_recommended_incentive VARCHAR(30),
    upstream_recommended_priority VARCHAR(10),
    last_outreach_date            DATE,
    last_outreach_channel         VARCHAR(20),
    is_suppressed                 VARCHAR(5),
    source_id                     VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS plan_population (
    plan_key       VARCHAR(20)  PRIMARY KEY,
    total_members  INTEGER,
    plan_revenue   BIGINT,
    last_updated   DATE
);

CREATE TABLE IF NOT EXISTS fact_nba_claude_decision (
    nba_run_id                VARCHAR(30),
    member_gap_key            VARCHAR(60),
    plan_key                  VARCHAR(20),
    measure_key               VARCHAR(20),
    nba_action_type           VARCHAR(50),
    cohort_id                 VARCHAR(30),
    cohort_name               VARCHAR(100),
    cohort_priority_rank      INTEGER,
    final_channel             VARCHAR(20),
    final_incentive           VARCHAR(30),
    priority_score            FLOAT,
    sla_days_to_contact       INTEGER,
    expected_gap_closure_lift FLOAT,
    reason_codes              VARCHAR(500),
    explanation_text          VARCHAR(2000),
    is_in_selected_opportunity INTEGER,
    created_at                TIMESTAMP_NTZ,
    PRIMARY KEY (nba_run_id, member_gap_key)
);

CREATE TABLE IF NOT EXISTS dim_nba_campaign (
    campaign_id               VARCHAR(30)  PRIMARY KEY,
    nba_run_id                VARCHAR(30),
    plan_key                  VARCHAR(20),
    measure_key               VARCHAR(20),
    channel_strategy          VARCHAR(500),
    frequency_plan            VARCHAR(500),
    incentive_strategy        VARCHAR(500),
    message_template          VARCHAR(2000),
    target_cohort_ids         VARCHAR(500),
    created_at                TIMESTAMP_NTZ
);

CREATE TABLE IF NOT EXISTS fact_nba_outreach_plan (
    outreach_id               VARCHAR(60)  PRIMARY KEY,
    nba_run_id                VARCHAR(30),
    campaign_id               VARCHAR(30),
    member_gap_key            VARCHAR(60),
    member_key                VARCHAR(30),
    plan_key                  VARCHAR(20),
    measure_key               VARCHAR(20),
    channel                   VARCHAR(20),
    planned_datetime          TIMESTAMP_NTZ,
    message_template          VARCHAR(2000),
    incentive_offered         VARCHAR(30),
    status                    VARCHAR(20),
    sent_at                   TIMESTAMP_NTZ,
    response_received         VARCHAR(5),
    notes                     VARCHAR(500),
    conversation_state        VARCHAR(30),
    outreach_sent_at          TIMESTAMP_NTZ,
    created_at                TIMESTAMP_NTZ
);

CREATE TABLE IF NOT EXISTS fact_nba_trace (
    trace_id                  VARCHAR(60)  PRIMARY KEY,
    nba_run_id                VARCHAR(30),
    agent_step                VARCHAR(50),
    agent_mode                VARCHAR(50),
    input_summary             VARCHAR(2000),
    output_summary            VARCHAR(2000),
    affected_population_count INTEGER,
    created_at                TIMESTAMP_NTZ
);
"""

if __name__ == "__main__":
    # Quick connectivity test
    print(f"DB_MODE = {DB_MODE}")
    try:
        with get_db() as conn:
            result = conn.execute("SELECT COUNT(*) AS n FROM dim_member").fetchone()
            n = result['n'] if isinstance(result, dict) else result[0]
            print(f"dim_member rows: {n:,}")
            result2 = conn.execute("SELECT COUNT(*) AS n FROM fact_member_gap").fetchone()
            n2 = result2['n'] if isinstance(result2, dict) else result2[0]
            print(f"fact_member_gap rows: {n2:,}")
        print("Connection OK.")
    except Exception as e:
        print(f"Connection FAILED: {e}")
