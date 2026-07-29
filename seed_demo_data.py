#!/usr/bin/env python3
"""
seed_demo_data.py — Standalone CareIntel demo database seeder.
Creates careintel.db from scratch with all tables and realistic demo data.
Safe to re-run: uses INSERT OR REPLACE / INSERT OR IGNORE throughout.
"""

import os
import sqlite3
import random
import uuid
from datetime import date, datetime, timedelta

random.seed(42)

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db"))
TODAY = str(date.today())
NOW   = datetime.now().isoformat(timespec="seconds")

print(f"[seed] DB path: {DB_PATH}")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")

# ── 1. Schema ─────────────────────────────────────────────────────────────────

conn.executescript("""
CREATE TABLE IF NOT EXISTS dim_measure (
    measure_key TEXT PRIMARY KEY,
    measure_code TEXT,
    measure_name TEXT,
    hedis_domain TEXT,
    star_weight REAL,
    eligibility_criteria TEXT
);

CREATE TABLE IF NOT EXISTS dim_plan_contract (
    plan_key TEXT PRIMARY KEY,
    plan_name TEXT,
    contract_id TEXT,
    region TEXT,
    segment TEXT,
    star_rating_current REAL,
    star_rating_target REAL,
    plan_annual_revenue REAL DEFAULT 350000000,
    total_members INTEGER DEFAULT 500,
    plan_pmpm_monthly REAL DEFAULT 1100,
    source_id TEXT DEFAULT 'demo'
);

CREATE TABLE IF NOT EXISTS dim_member (
    member_key TEXT PRIMARY KEY,
    plan_key TEXT,
    age_band TEXT,
    gender TEXT,
    language_preference TEXT,
    digital_literacy_segment TEXT,
    socioeconomic_segment TEXT,
    display_name TEXT,
    source_id TEXT DEFAULT 'demo'
);

CREATE TABLE IF NOT EXISTS dim_member_channel_pref (
    member_key TEXT PRIMARY KEY,
    email_allowed TEXT,
    sms_allowed TEXT,
    call_allowed TEXT,
    preferred_channel TEXT,
    do_not_contact_flag TEXT,
    channel_risk_notes TEXT
);

CREATE TABLE IF NOT EXISTS fact_member_gap (
    member_gap_key TEXT PRIMARY KEY,
    member_key TEXT,
    measure_key TEXT,
    measure_code TEXT,
    plan_key TEXT,
    gap_status TEXT,
    measurement_year INTEGER,
    gap_open_date TEXT,
    gap_close_date TEXT,
    days_open INTEGER,
    nba_propensity_score REAL,
    clinical_risk_score REAL,
    previous_year_gap_flag TEXT,
    upstream_recommended_channel TEXT,
    upstream_recommended_incentive TEXT,
    upstream_recommended_priority TEXT,
    last_outreach_date TEXT,
    last_outreach_channel TEXT,
    is_suppressed TEXT DEFAULT 'false',
    source_id TEXT DEFAULT 'demo'
);

CREATE TABLE IF NOT EXISTS fact_nba_claude_decision (
    member_gap_key TEXT,
    nba_run_id TEXT,
    cohort_id TEXT,
    cohort_name TEXT,
    cohort_priority_rank INTEGER,
    nba_action_type TEXT,
    final_channel TEXT,
    final_incentive TEXT,
    priority_score REAL,
    sla_days_to_contact INTEGER,
    expected_gap_closure_lift REAL,
    reason_codes TEXT,
    explanation_text TEXT,
    is_in_selected_opportunity TEXT,
    PRIMARY KEY (member_gap_key, nba_run_id)
);

CREATE TABLE IF NOT EXISTS dim_nba_campaign (
    campaign_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    plan_key TEXT,
    measure_key TEXT,
    channel_strategy TEXT,
    frequency_plan TEXT,
    incentive_strategy TEXT,
    message_template TEXT,
    target_cohort_ids TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS fact_nba_outreach_plan (
    contact_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    member_gap_key TEXT,
    campaign_id TEXT,
    channel TEXT,
    planned_datetime TEXT,
    message_template_id TEXT,
    incentive_offered TEXT,
    status TEXT DEFAULT 'PLANNED',
    generated_message TEXT,
    sent_at TEXT,
    error_reason TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS fact_nba_trace (
    trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nba_run_id TEXT,
    timestamp TEXT,
    agent TEXT,
    step TEXT,
    input_summary TEXT,
    output_summary TEXT,
    affected_population_count INTEGER
);

CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    conversation_id TEXT PRIMARY KEY,
    member_gap_key TEXT,
    contact_id TEXT,
    nba_run_id TEXT,
    member_phone TEXT,
    member_key TEXT,
    measure_name TEXT,
    conversation_state TEXT DEFAULT 'OUTREACH_SENT',
    appointment_date TEXT,
    follow_up_sent INTEGER DEFAULT 0,
    gap_closed INTEGER DEFAULT 0,
    last_inbound_msg TEXT,
    created_timestamp TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS campaign_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    campaign_id TEXT,
    evaluation_date TEXT,
    evaluation_window INTEGER,
    measure_code TEXT,
    total_members_contacted INTEGER,
    gaps_closed_actual INTEGER,
    gaps_closed_expected INTEGER,
    actual_closure_rate REAL,
    expected_closure_rate REAL,
    performance_status TEXT,
    stars_impact_actual REAL,
    stars_impact_projected REAL,
    executive_summary TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS member_evaluations (
    member_eval_id TEXT PRIMARY KEY,
    evaluation_id TEXT,
    nba_run_id TEXT,
    contact_id TEXT,
    member_gap_key TEXT,
    member_key TEXT,
    outreach_sent_date TEXT,
    gap_status_at_evaluation TEXT,
    days_since_outreach INTEGER,
    responded INTEGER DEFAULT 0,
    recommended_action TEXT,
    action_reason TEXT,
    follow_up_scheduled TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_schedule (
    schedule_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    campaign_id TEXT,
    scheduled_date TEXT,
    evaluation_window INTEGER,
    status TEXT DEFAULT 'PENDING',
    created_timestamp TEXT
);

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
conn.commit()
print("[seed] Tables created")

# ── 2. Measures ───────────────────────────────────────────────────────────────

MEASURES = [
    ("M001", "BCS",  "Breast Cancer Screening",              "Preventive Screening", 3.0),
    ("M002", "COL",  "Colorectal Cancer Screening",          "Preventive Screening", 3.0),
    ("M003", "EED",  "Eye Exam for Patients with Diabetes",  "Diabetes Care",        1.0),
    ("M004", "CDC",  "Comprehensive Diabetes Care",          "Diabetes Care",        2.0),
    ("M005", "MAD",  "Medication Adherence for Diabetes",    "Medication Adherence", 2.0),
    ("M006", "AFV",  "Annual Flu Vaccine",                   "Preventive Care",      1.0),
    ("M007", "SPC",  "Statin Therapy for CVD",               "Cardiovascular Care",  2.0),
]

for mk, mc, mn, domain, sw in MEASURES:
    conn.execute(
        "INSERT OR REPLACE INTO dim_measure (measure_key,measure_code,measure_name,hedis_domain,star_weight) VALUES (?,?,?,?,?)",
        (mk, mc, mn, domain, sw)
    )

# Benchmarks
BENCH = {
    "M001": (0.74, 0.82, 0.65), "M002": (0.68, 0.78, 0.58), "M003": (0.72, 0.81, 0.62),
    "M004": (0.69, 0.79, 0.58), "M005": (0.78, 0.86, 0.70), "M006": (0.80, 0.88, 0.72),
    "M007": (0.65, 0.75, 0.55),
}
for mk, (avg, top, bot) in BENCH.items():
    conn.execute(
        "INSERT OR REPLACE INTO measure_benchmarks VALUES (?,?,?,?,?,?,?)",
        (mk, 2026, avg, top, bot, "NCQA HEDIS 2024", TODAY)
    )

# Costs
COSTS = [
    ("T1_EMAIL", 1, "EMAIL", 1.50, 0, 1.50), ("T1_SMS", 1, "SMS", 0.50, 0, 0.50),
    ("T2_SMS",   2, "SMS",   0.50, 15, 15.50), ("T2_EMAIL", 2, "EMAIL", 1.50, 15, 16.50),
    ("T3_CALL",  3, "CALL",  8.00, 25, 33.00), ("T3_WA",    3, "WHATSAPP", 0.10, 25, 25.10),
]
for r in COSTS:
    conn.execute("INSERT OR REPLACE INTO outreach_costs VALUES (?,?,?,?,?,?,?)", (*r, TODAY))

# Closure rate assumptions
for mk in [m[0] for m in MEASURES]:
    for tier, rate in [(1, 0.60), (2, 0.35), (3, 0.18)]:
        conn.execute(
            "INSERT OR REPLACE INTO closure_rate_assumptions VALUES (?,?,?,?,?,?)",
            (f"{mk}_T{tier}", mk, tier, rate, "industry_default", TODAY)
        )

conn.commit()
print("[seed] Measures, benchmarks, costs seeded")

# ── 3. Plans ──────────────────────────────────────────────────────────────────

PLANS = [
    ("P001", "Aetna Medicare Choice PPO (Northeast)",    "H1234", "Northeast", "MAPD", 3.5, 4.0, 350_000_000, 520, 1120),
    ("P002", "Aetna Medicare Premier PPO (Southeast)",   "H1235", "Southeast", "MAPD", 4.0, 4.5, 420_000_000, 480, 1180),
    ("P003", "Aetna Medicare DSNP Community (Midwest)",  "H1236", "Midwest",   "DSNP", 3.0, 4.0, 180_000_000, 310, 980),
    ("P004", "UHC Medicare Advantage Value (West)",      "H5678", "West",      "MAPD", 4.5, 5.0, 290_000_000, 430, 1050),
    ("P005", "UHC Medicare Signature PPO (West)",        "H5679", "West",      "MAPD", 2.5, 3.0, 260_000_000, 390, 1080),
]

for p in PLANS:
    conn.execute(
        "INSERT OR REPLACE INTO dim_plan_contract (plan_key,plan_name,contract_id,region,segment,star_rating_current,star_rating_target,plan_annual_revenue,total_members,plan_pmpm_monthly) VALUES (?,?,?,?,?,?,?,?,?,?)", p
    )
conn.commit()
print("[seed] Plans seeded")

# ── 4. Members ────────────────────────────────────────────────────────────────

EN_NAMES = [
    "Richard Brown","Edna Sullivan","Catherine Stewart","James Wilson","Mary Johnson",
    "Robert Davis","Patricia Miller","Michael Anderson","Linda Taylor","William Thomas",
    "Barbara Jackson","David White","Susan Harris","Joseph Martin","Jessica Thompson",
    "Charles Garcia","Sarah Martinez","Joseph Robinson","Karen Clark","Steven Lewis",
    "Betty Lee","Edward Walker","Dorothy Hall","Brian Allen","Sandra Young",
    "George Hernandez","Ashley King","Joshua Wright","Kimberly Lopez","Andrew Hill",
    "Donna Scott","Mark Green","Carol Adams","Paul Baker","Ruth Gonzalez",
    "Donald Nelson","Sharon Carter","Kevin Mitchell","Dorothy Perez","Timothy Roberts",
    "Helen Turner","Jeffrey Phillips","Virginia Campbell","Ryan Parker","Kathleen Evans",
    "Jacob Edwards","Shirley Collins","Gary Stewart","Janet Sanchez","Nicholas Morris",
    "Carolyn Rogers","Eric Reed","Ruth Cook","Jonathan Morgan","Maria Bell",
    "Stephen Murphy","Diane Bailey","Larry Rivera","Rebecca Cooper","Frank Richardson",
    "Christine Cox","Scott Howard","Evelyn Ward","Raymond Torres","Hannah Peterson",
    "Gregory Gray","Margaret Ramirez","Jerry James","Cynthia Watson","Dennis Brooks",
    "Martha Kelly","Walter Price","Joyce Bennett","Peter Wood","Frances Barnes",
    "Harold Ross","Ann Henderson","Wayne Coleman","Marilyn Jenkins","Russell Perry",
    "Cheryl Powell","Roy Long","Deborah Patterson","Billy Hughes","Rachel Flores",
    "Eugene Washington","Judith Butler","Arthur Simmons","Rose Foster","Philip Gonzales",
    "Laura Bryant","Albert Alexander","Lois Russell","Howard Griffin","Marie Diaz",
    "Fred Hayes","Judith Myers","Ralph Ford","Virginia Hamilton","Clarence Graham",
    "Kathryn Sullivan","Fred Wallace","Brenda West","Earl Cole","Ann Spencer",
]

ES_NAMES = [
    "Maria Garcia","Carlos Rodriguez","Ana Martinez","Jose Hernandez","Lucia Lopez",
    "Miguel Gonzalez","Sofia Perez","Juan Torres","Isabella Ramirez","Diego Flores",
    "Carmen Ruiz","Pedro Morales","Gabriela Jimenez","Fernando Alvarez","Rosa Romero",
    "Eduardo Vargas","Alejandra Castillo","Roberto Soto","Patricia Mendez","Antonio Reyes",
    "Valentina Cruz","Jorge Ortega","Claudia Navarro","Alberto Dominguez","Adriana Ramos",
    "Oscar Herrera","Laura Medina","Sergio Aguilar","Monica Vega","Hector Guzman",
    "Daniela Rojas","Pablo Moreno","Elena Guerrero","Manuel Delgado","Beatriz Espinoza",
]

ZH_NAMES = [
    "Li Wei","Wang Fang","Zhang Min","Liu Yang","Chen Jing",
    "Huang Xia","Zhao Lei","Zhou Ying","Wu Gang","Xu Hong",
    "Sun Ping","Ma Qiang","Zhu Mei","Hu Lin","Guo Yan",
    "He Jun","Lin Tao","Gao Ning","Luo Hui","Zheng Bo",
    "Cao Xue","Song Jian","Tang Yan","Feng Wei","Dong Hua",
]

AGE_BANDS    = ["65-69","70-74","75-79","80-84","85+"]
DIG_LIT      = ["High","Medium","Low"]
SES_SEGS     = ["High","Mid","Low"]
PREF_CHNLS   = ["EMAIL","SMS","CALL"]

# Distribute ~300 members across plans, weighted to P001/P005 (bigger plans)
PLAN_WEIGHTS = {"P001":60,"P002":50,"P003":40,"P004":50,"P005":50}
LANG_MIX     = {"P001":[("EN",0.80),("ES",0.15),("ZH",0.05)],
                 "P002":[("EN",0.85),("ES",0.12),("ZH",0.03)],
                 "P003":[("EN",0.60),("ES",0.35),("ZH",0.05)],
                 "P004":[("EN",0.75),("ES",0.15),("ZH",0.10)],
                 "P005":[("EN",0.70),("ES",0.20),("ZH",0.10)]}

en_pool = EN_NAMES[:]
es_idx  = 0
zh_idx  = 0

members = []
m_idx   = 1
for plan_key, count in PLAN_WEIGHTS.items():
    lang_dist = LANG_MIX[plan_key]
    for _ in range(count):
        mk  = f"MBR{m_idx:05d}"
        r   = random.random()
        cum = 0.0
        lang = "EN"
        for l, w in lang_dist:
            cum += w
            if r < cum:
                lang = l
                break
        if lang == "EN":
            if en_pool:
                name = en_pool.pop(random.randint(0, len(en_pool)-1))
            else:
                name = f"Member {mk}"
        elif lang == "ES":
            name = ES_NAMES[es_idx % len(ES_NAMES)]; es_idx += 1
        else:
            name = ZH_NAMES[zh_idx % len(ZH_NAMES)]; zh_idx += 1

        dig = random.choices(DIG_LIT, weights=[0.35,0.40,0.25])[0]
        ses = random.choices(SES_SEGS, weights=[0.25,0.50,0.25])[0]
        age = random.choice(AGE_BANDS)

        members.append((mk, plan_key, age, "F" if random.random() < 0.55 else "M",
                        lang, dig, ses, name))
        m_idx += 1

conn.executemany(
    "INSERT OR REPLACE INTO dim_member (member_key,age_band,gender,language_preference,digital_literacy_segment,socioeconomic_segment,display_name) VALUES (?,?,?,?,?,?,?)", [(mk,age,g,lang,dig,ses,name) for mk,_pk,age,g,lang,dig,ses,name in members]
)

# Channel prefs
ch_rows = []
for mk, plan_key, age, gender, lang, dig, ses, name in members:
    email_ok = "true"  if random.random() < 0.70 else "false"
    sms_ok   = "true"  if random.random() < 0.75 else "false"
    call_ok  = "true"  if random.random() < 0.80 else "false"
    dnc      = "true"  if random.random() < 0.03 else "false"
    if dig == "High":
        pref = random.choices(["EMAIL","SMS"], weights=[0.60,0.40])[0]
    elif dig == "Low" or lang != "EN":
        pref = "CALL"
    else:
        pref = random.choice(PREF_CHNLS)
    ch_rows.append((mk, email_ok, sms_ok, call_ok, pref, dnc, ""))

conn.executemany(
    "INSERT OR REPLACE INTO dim_member_channel_pref VALUES (?,?,?,?,?,?,?)", ch_rows
)
conn.commit()
print(f"[seed] {len(members)} members + channel prefs seeded")

# ── 5. Gaps ───────────────────────────────────────────────────────────────────

# Compliance rates by plan (star-correlated)
COMPLIANCE = {"P001":0.60,"P002":0.68,"P003":0.50,"P004":0.73,"P005":0.38}
# Measures each plan is sampled against
ALL_MKS = [m[0] for m in MEASURES]

gaps = []
g_idx = 1
member_by_plan = {}
for mk, plan_key, *_ in members:
    member_by_plan.setdefault(plan_key, []).append(mk)

for plan_key, mlist in member_by_plan.items():
    comp_rate = COMPLIANCE[plan_key]
    for member_key in mlist:
        # Each member gets 2-3 gap rows across random measures
        n_gaps = random.choices([2, 3], weights=[0.55, 0.45])[0]
        chosen_mks = random.sample(ALL_MKS, n_gaps)
        for measure_key in chosen_mks:
            measure_code = next(m[1] for m in MEASURES if m[0] == measure_key)
            gk = f"G{g_idx:05d}"
            closed = random.random() < comp_rate
            status = "Closed" if closed else random.choices(
                ["Open","Borderline"], weights=[0.85,0.15])[0]
            days_open = 0 if closed else random.randint(30, 340)
            prop = round(random.uniform(0.30, 0.90), 3)
            risk = round(random.uniform(0.20, 0.95), 3)
            prev_gap = "true" if random.random() < 0.45 else "false"
            gaps.append((gk, member_key, measure_key, measure_code, plan_key,
                         status, 2025, days_open, prop, risk, prev_gap, "false"))
            g_idx += 1

conn.executemany(
    "INSERT OR REPLACE INTO fact_member_gap (member_gap_key,member_key,measure_key,measure_code,plan_key,gap_status,measurement_year,days_open,nba_propensity_score,clinical_risk_score,previous_year_gap_flag,is_suppressed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", gaps
)
conn.commit()
print(f"[seed] {len(gaps)} gap rows seeded")

# ── 6. NBA Sessions ───────────────────────────────────────────────────────────

# 4 historical sessions matching the demo narrative
SESSIONS = [
    {
        "run_id": "RUN_20260708_091523",
        "measure_key": "M003", "measure_code": "EED",
        "plan_key": "P005",
        "days_ago_sent": 7,
        "response_rate": 0.09,
        "perf_status": "Underperforming",
        "campaign_id": "CAMP_RUN_20260708_091523",
    },
    {
        "run_id": "RUN_20260710_140812",
        "measure_key": "M004", "measure_code": "CDC",
        "plan_key": "P003",
        "days_ago_sent": 5,
        "response_rate": 0.25,
        "perf_status": "On Track",
        "campaign_id": "CAMP_RUN_20260710_140812",
    },
    {
        "run_id": "RUN_20260712_103045",
        "measure_key": "M005", "measure_code": "MAD",
        "plan_key": "P004",
        "days_ago_sent": 3,
        "response_rate": 0.55,
        "perf_status": "Overperforming",
        "campaign_id": "CAMP_RUN_20260712_103045",
    },
    {
        "run_id": "RUN_20260714_154231",
        "measure_key": "M001", "measure_code": "BCS",
        "plan_key": "P001",
        "days_ago_sent": 1,
        "response_rate": 0.12,
        "perf_status": "On Track",
        "campaign_id": "CAMP_RUN_20260714_154231",
    },
]

PLAN_NAMES = {p[0]: p[1] for p in PLANS}
PLAN_STARS = {p[0]: (p[5], p[6]) for p in PLANS}
MEASURE_NAMES = {m[0]: m[2] for m in MEASURES}

CONV_STATES = ["COMPLETED","DATE_CONFIRMED","AWAITING_DATE","FOLLOW_UP_SENT","OUTREACH_SENT","DECLINED"]
CONV_MSGS = {
    "COMPLETED":      "Thank you! Your appointment has been completed.",
    "DATE_CONFIRMED": "Great! Your appointment is confirmed.",
    "AWAITING_DATE":  "Thank you! Can you share a date that works for your appointment?",
    "FOLLOW_UP_SENT": "Just following up — were you able to complete your appointment?",
    "OUTREACH_SENT":  "Hello! We're reaching out about an important health screening.",
    "DECLINED":       "I understand. Please reach out if you change your mind.",
}

test_phone = os.getenv("TEST_SMS_NUMBER", "+19999999999")

for sess in SESSIONS:
    run_id     = sess["run_id"]
    mk         = sess["measure_key"]
    mc         = sess["measure_code"]
    pk         = sess["plan_key"]
    camp_id    = sess["campaign_id"]
    days_sent  = sess["days_ago_sent"]
    resp_rate  = sess["response_rate"]
    perf       = sess["perf_status"]
    sent_date  = str(date.today() - timedelta(days=days_sent))
    sent_ts    = f"{sent_date}T10:00:00"

    # Get open gaps for this measure×plan
    open_gaps = conn.execute(
        "SELECT * FROM fact_member_gap WHERE measure_key=? AND plan_key=? AND LOWER(gap_status) IN ('open','borderline')",
        (mk, pk)
    ).fetchall()
    random.shuffle(open_gaps)
    target_gaps = open_gaps[:40]  # cap at 40 contacts per session

    if not target_gaps:
        print(f"[seed] WARNING: no open gaps for {mc}×{pk}, skipping session {run_id}")
        continue

    # Trace
    plan_name = PLAN_NAMES[pk]
    meas_name = MEASURE_NAMES[mk]

    for step, agent, summary, inp in [
        ("OPPORTUNITY_SELECTED", "OpportunityAgent",
         f"{mc} x {plan_name} selected; {len(target_gaps)} addressable gaps; star_weight={next(m[4] for m in MEASURES if m[0]==mk)}",
         "Scanned all measure x plan combinations"),
        ("COHORTS_DEFINED", "SegmentationAgent",
         "C1_DIGITAL_HIGH_PROP(10); C2_ACCESS_BARRIER(30)",
         f"{len(target_gaps)} in-scope gaps"),
        ("CAMPAIGN_DESIGNED", "CampaignAgent",
         f"{camp_id}; SMS primary; GIFTCARD_25",
         "Cohorts C1, C2 selected"),
        ("OUTREACH_PLANNED", "OutreachAgent",
         f"{len(target_gaps)} planned contacts",
         f"Campaign {camp_id} approved"),
        ("RUN_SUMMARY", "System",
         f"Run complete — {len(target_gaps)} contacts planned",
         run_id),
    ]:
        conn.execute(
            "INSERT INTO fact_nba_trace (nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count) VALUES (?,?,?,?,?,?,?)",
            (run_id, sent_ts, agent, step, inp, summary, len(target_gaps))
        )

    # Campaign
    conn.execute(
        """INSERT OR REPLACE INTO dim_nba_campaign
           (campaign_id,nba_run_id,plan_key,measure_key,channel_strategy,frequency_plan,
            incentive_strategy,message_template_id,target_cohort_ids,created_timestamp,campaign_name)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (camp_id, run_id, pk, mk,
         "SMS primary; CALL fallback",
         "SMS d1 → d7 → call d14; stop on close",
         "GIFTCARD_25 for all cohorts",
         f"TMPL_{mc}_PLAIN_V1",
         "C1_DIGITAL_HIGH_PROP,C2_ACCESS_BARRIER",
         sent_ts,
         f"{mc} Outreach Campaign")
    )

    # Outreach plan + decisions + conversations
    n_closed = int(round(len(target_gaps) * resp_rate))
    closed_indices = set(random.sample(range(len(target_gaps)), n_closed))

    for i, gap in enumerate(target_gaps):
        gap = dict(gap)
        gk      = gap["member_gap_key"]
        mem_key = gap["member_key"]
        contact_id = f"CTX_{run_id[4:]}_{gk}"
        is_closed  = i in closed_indices
        gap_status = "Closed" if is_closed else gap["gap_status"]

        if is_closed:
            conn.execute("UPDATE fact_member_gap SET gap_status='Closed' WHERE member_gap_key=?", (gk,))

        cohort = "C1_DIGITAL_HIGH_PROP" if i < 10 else "C2_ACCESS_BARRIER"
        channel = "SMS" if i < 10 else "CALL"
        incentive = "GIFTCARD_25"

        conn.execute(
            """INSERT OR REPLACE INTO fact_nba_claude_decision
               (member_gap_key,nba_run_id,cohort_id,cohort_name,cohort_priority_rank,
                nba_action_type,final_channel,final_incentive,priority_score,
                sla_days_to_contact,expected_gap_closure_lift,reason_codes,
                explanation_text,is_in_selected_opportunity,member_key,measure_key,
                measure_code,plan_key,measurement_year,decision_timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gk, run_id, cohort, cohort.replace("_"," ").title(), 1 if cohort.startswith("C1") else 2,
             "OUTREACH_MEMBER", channel, incentive,
             round(gap["nba_propensity_score"]*100), 7, resp_rate,
             "HIGH_PROP,ELIGIBLE", f"Member targeted for {mc} gap closure",
             "true", mem_key, gap.get("measure_key",""), mc, pk, 2026, sent_ts)
        )

        conn.execute(
            "INSERT OR REPLACE INTO fact_nba_outreach_plan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contact_id, run_id, gk, camp_id, channel,
             f"{sent_date}T09:00:00", f"TMPL_{mc}_PLAIN_V1", incentive,
             "SENT", f"Hi, reminder to complete your {meas_name} this year.",
             sent_ts, None, sent_ts)
        )

        # WhatsApp conversation
        if is_closed:
            state = "COMPLETED"
            appt  = str(date.today() - timedelta(days=random.randint(1, days_sent)))
        else:
            state = random.choices(
                CONV_STATES[1:],
                weights=[0.25, 0.20, 0.20, 0.15, 0.05]
            )[0]
            appt = str(date.today() + timedelta(days=random.randint(1, 14))) if state in ("DATE_CONFIRMED","FOLLOW_UP_SENT") else None

        conn.execute(
            """INSERT OR IGNORE INTO whatsapp_conversations
               (conversation_id,member_gap_key,contact_id,nba_run_id,member_phone,
                member_key,measure_name,conversation_state,appointment_date,
                follow_up_sent,gap_closed,last_inbound_msg,created_timestamp,
                last_updated,channel)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"CONV_{contact_id}", gk, contact_id, run_id,
             test_phone, mem_key, meas_name,
             state, appt,
             1 if state == "COMPLETED" else 0,
             1 if is_closed else 0,
             CONV_MSGS[state],
             sent_ts, sent_ts, channel)
        )

    conn.commit()

    # Campaign evaluation
    total   = len(target_gaps)
    closed  = n_closed
    window  = 7
    _HEDIS7 = {"EED":0.15,"CDC":0.25,"MAD":0.45,"BCS":0.12,"COL":0.10,"AFV":0.35,"SPC":0.40}
    exp_rate   = _HEDIS7.get(mc, 0.20)
    act_rate   = round(closed / total, 3) if total else 0.0
    exp_closed = max(1, round(total * exp_rate))
    denom      = 75.0
    sw         = next(m[4] for m in MEASURES if m[0] == mk)
    spg        = (1 / denom) * sw * 0.5
    stars_act  = round(min(closed * spg, 0.50), 4)
    stars_proj = round(min(exp_closed * spg, 0.50), 4)

    eval_id = f"EVAL_{run_id[4:]}_{window}D_{TODAY}"
    summary_txt = (
        f"Campaign {camp_id} evaluated at day {window}. "
        f"{closed}/{total} members closed their gap "
        f"({round(act_rate*100)}% actual vs {round(exp_rate*100)}% expected). "
        f"Status: {perf}."
    )

    conn.execute(
        """INSERT OR REPLACE INTO campaign_evaluations
           (evaluation_id,nba_run_id,campaign_id,evaluation_date,evaluation_window,measure_code,
            total_members_contacted,gaps_closed_actual,gaps_closed_expected,
            actual_closure_rate,expected_closure_rate,performance_status,
            stars_impact_actual,stars_impact_projected,executive_summary,created_timestamp,
            plan_key,plan_name,measure_name,outreach_date,star_rating_current,star_rating_target)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eval_id, run_id, camp_id, TODAY, window, mc,
         total, closed, exp_closed, act_rate, exp_rate, perf,
         stars_act, stars_proj, summary_txt, NOW,
         pk, plan_name, meas_name, sent_date,
         PLAN_STARS.get(pk, (3.5, 4.0))[0], PLAN_STARS.get(pk, (3.5, 4.0))[1])
    )

    # Member evaluations
    all_contacts = conn.execute(
        """SELECT o.contact_id, o.member_gap_key, g.member_key
           FROM fact_nba_outreach_plan o
           LEFT JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
           WHERE o.nba_run_id=?""",
        (run_id,)
    ).fetchall()
    for c in all_contacts:
        gap_s = conn.execute("SELECT gap_status FROM fact_member_gap WHERE member_gap_key=?",
                             (c["member_gap_key"],)).fetchone()
        gs = gap_s["gap_status"] if gap_s else "Open"
        responded = 1 if gs == "Closed" else 0
        action = "NO_ACTION" if gs == "Closed" else "EXTEND_CAMPAIGN"
        reason = "Gap already closed" if gs == "Closed" else "Within initial window"
        conn.execute(
            "INSERT OR REPLACE INTO member_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"MEVAL_{run_id[4:]}_{c['contact_id']}_{TODAY}",
             eval_id, run_id, c["contact_id"], c["member_gap_key"],
             c["member_key"], sent_date, gs, days_sent, responded,
             action, reason,
             str(date.today() + timedelta(days=7)) if action != "NO_ACTION" else "",
             NOW)
        )

    conn.commit()
    print(f"[seed] Session {run_id}: {total} contacts, {closed} closed ({round(act_rate*100)}%), {perf}")

# ── 7. Summary ────────────────────────────────────────────────────────────────

print()
print("=" * 55)
print("  CareIntel Demo Database — Seed Complete")
print("=" * 55)
tables_to_check = [
    "dim_plan_contract","dim_measure","dim_member","dim_member_channel_pref",
    "fact_member_gap","fact_nba_claude_decision","dim_nba_campaign",
    "fact_nba_outreach_plan","fact_nba_trace","whatsapp_conversations",
    "campaign_evaluations","member_evaluations",
]
for t in tables_to_check:
    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t:<35} {n:>6} rows")
print("=" * 55)
print(f"  DB: {DB_PATH}")
print(f"  Size: {round(os.path.getsize(DB_PATH)/1024)} KB")
print("=" * 55)

conn.close()
