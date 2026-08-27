"""
Creates a second Snowflake database (MERIDIAN_HEALTH) with a completely
different set of plans, members, and gaps — large synthetic dataset.
Run: python create_new_sf_db.py
"""
import os, sys, random, uuid, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dotenv
dotenv.load_dotenv()

from db_adapter import _SnowflakeConn

NEW_DB    = "MERIDIAN_HEALTH"
SCHEMA    = "STARS"
WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")

random.seed(42)

# ── Plan definitions (completely different from CAREINTEL) ────────────────────
PLANS = [
    ("MP001", "MR-2001", "BlueStar Premier",        "Northeast", "Individual", 3.5, 4.0, 112_400_000, 9800,  1180),
    ("MP002", "MR-2002", "SilverCare Elite",         "Southeast", "Group",      3.0, 4.0,  87_600_000, 7200,  1210),
    ("MP003", "MR-2003", "HorizonHealth Gold",       "Midwest",   "Individual", 4.0, 4.5, 154_800_000,12300,  1260),
    ("MP004", "MR-2004", "PrimeWell Advantage",      "Southwest", "Individual", 2.5, 3.5,  63_500_000, 5500,  1150),
    ("MP005", "MR-2005", "ClearPath Medicare",       "West",      "Group",      3.5, 4.0,  98_700_000, 8600,  1140),
    ("MP006", "MR-2006", "Apex Senior Care",         "Mountain",  "Individual", 4.0, 4.5, 168_200_000,13100,  1290),
    ("MP007", "MR-2007", "Vanguard Health Partners", "Pacific",   "Group",      3.0, 3.5,  74_300_000, 6400,  1160),
]

# ── Measure definitions ───────────────────────────────────────────────────────
MEASURES = [
    ("MX001","BCS","Breast Cancer Screening",             "Effectiveness",2.0,"Female 52-74","Annual mammography screening"),
    ("MX002","COL","Colorectal Cancer Screening",         "Effectiveness",1.5,"All 45-75","Colonoscopy/stool test"),
    ("MX003","EED","Eye Exam for Diabetics",              "Effectiveness",1.5,"Diabetic members","Annual retinal exam"),
    ("MX004","CBP","Controlling High Blood Pressure",     "Effectiveness",3.0,"Hypertension dx","BP <140/90"),
    ("MX005","CDC","Diabetes Care - Blood Sugar Control", "Effectiveness",3.0,"Diabetic members","HbA1c < 8%"),
    ("MX006","MPM","Medication Adherence - Blood Pressure","Effectiveness",1.5,"Antihypertensive Rx","PDC ≥ 0.80"),
    ("MX007","MPD","Medication Adherence - Diabetes",     "Effectiveness",1.5,"Diabetes Rx","PDC ≥ 0.80"),
    ("MX008","PPC","Prenatal & Postpartum Care",          "Effectiveness",1.0,"Pregnant members","Timely prenatal visit"),
    ("MX009","FUH","Follow-Up After Hospitalization",     "Effectiveness",1.0,"Post-inpatient","7-day follow-up"),
    ("MX010","AWC","Annual Wellness Check",               "Effectiveness",1.0,"All eligible","Preventive visit"),
]

# ── Member generation ─────────────────────────────────────────────────────────
FIRST_NAMES = ["James","Maria","Robert","Patricia","William","Linda","Michael","Barbara",
               "David","Susan","Richard","Jessica","Joseph","Sarah","Thomas","Karen",
               "Charles","Lisa","Christopher","Nancy","Daniel","Dorothy","Matthew","Betty",
               "Anthony","Sandra","Mark","Ashley","Donald","Margaret","Steven","Kimberly",
               "Paul","Emily","Andrew","Donna","Joshua","Michelle","Kenneth","Carol",
               "Kevin","Amanda","Brian","Melissa","George","Deborah","Timothy","Stephanie",
               "Ronald","Rebecca","Edward","Sharon","Jason","Laura","Jeffrey","Cynthia",
               "Ryan","Kathleen","Jacob","Amy","Gary","Angela","Nicholas","Shirley",
               "Eric","Emma","Jonathan","Brenda","Stephen","Pamela","Larry","Emma",
               "Justin","Virginia","Scott","Evelyn","Frank","Joyce","Brandon","Victoria"]

LAST_NAMES = ["Anderson","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
              "Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Thomas","Taylor",
              "Moore","Jackson","Martin","Lee","Perez","Thompson","White","Harris","Sanchez",
              "Clark","Ramirez","Lewis","Robinson","Walker","Young","Allen","King","Wright",
              "Scott","Torres","Nguyen","Hill","Flores","Green","Adams","Nelson","Baker",
              "Hall","Rivera","Campbell","Mitchell","Carter","Roberts","Phillips","Evans",
              "Turner","Torres","Parker","Collins","Edwards","Stewart","Flores","Morris"]

LANGS    = ["English","Spanish","Portuguese","Mandarin","Vietnamese","Korean","Tagalog","Arabic","French","Russian"]
LITERACY = ["high","medium","low"]
SOCIO    = ["high","middle","low"]
CHANNELS = ["EMAIL","SMS","CALL"]

def gen_member(i, plan_key):
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    age = random.randint(52, 84)
    gender = random.choice(["M","F","F","F"])  # slight female skew for BCS
    lang = random.choices(LANGS, weights=[60,15,5,4,4,3,3,2,2,2])[0]
    lit  = random.choices(LITERACY, weights=[45,35,20])[0]
    soc  = random.choices(SOCIO, weights=[25,55,20])[0]
    pref = random.choices(CHANNELS, weights=[35,40,25])[0]
    email_ok = "true" if random.random() > 0.2 else "false"
    sms_ok   = "true" if random.random() > 0.25 else "false"
    call_ok  = "true" if random.random() > 0.15 else "false"
    dnc      = "true" if random.random() < 0.05 else "false"
    return {
        "member_key": f"MBR_{plan_key}_{i:05d}",
        "plan_key": plan_key,
        "dob_year": 2026 - age,
        "age_band": f"{(age//10)*10}s",
        "gender": gender,
        "language_preference": lang,
        "digital_literacy_segment": lit,
        "socioeconomic_segment": soc,
        "display_name": f"{fn} {ln}",
        "email_allowed": email_ok,
        "sms_allowed": sms_ok,
        "call_allowed": call_ok,
        "preferred_channel": pref,
        "do_not_contact_flag": dnc,
    }

def gen_gap(member, measure_key, measure_code, year=2025):
    days_open = random.randint(0, 720)
    status = random.choices(["OPEN","CLOSED"], weights=[55,45])[0]
    propensity = round(random.betavariate(2, 3), 4)
    rec_channel = random.choice(["SMS","EMAIL","CALL"])
    rec_incentive = random.choice(["GIFTCARD_25","GIFTCARD_50","TRANSPORT_VOUCHER","NONE"])
    rec_priority  = random.choice(["HIGH","MEDIUM","LOW"])
    open_date = (datetime.date(2025,1,1) + datetime.timedelta(days=random.randint(0,300))).isoformat()
    return {
        "member_gap_key": f"GAP_{member['member_key']}_{measure_code}_{year}",
        "member_key": member["member_key"],
        "measure_key": measure_key,
        "measure_code": measure_code,
        "plan_key": member["plan_key"],
        "measurement_year": year,
        "gap_status": status,
        "gap_open_date": open_date,
        "gap_close_date": None if status=="OPEN" else (datetime.date(2025,6,1)+datetime.timedelta(days=random.randint(0,180))).isoformat(),
        "days_open": days_open if status=="OPEN" else 0,
        "clinical_risk_score": round(random.betavariate(2,5), 4),
        "nba_propensity_score": propensity,
        "previous_year_gap_flag": random.choice([0,0,1]),
        "upstream_recommended_channel": rec_channel,
        "upstream_recommended_incentive": rec_incentive,
        "upstream_recommended_priority": rec_priority,
        "last_outreach_date": None,
        "last_outreach_channel": None,
        "is_suppressed": "false",
        "source_id": "meridian_sf",
    }

# ── Connect and build ─────────────────────────────────────────────────────────
print(f"Connecting to Snowflake...")
raw_conn = _SnowflakeConn()
cur = raw_conn._conn.cursor()

print(f"Creating database {NEW_DB}...")
cur.execute(f"CREATE DATABASE IF NOT EXISTS {NEW_DB}")
cur.execute(f"USE DATABASE {NEW_DB}")
cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
cur.execute(f"USE SCHEMA {SCHEMA}")
cur.execute(f"USE WAREHOUSE {WAREHOUSE}")

print("Creating tables...")
cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.DIM_MEASURE (
    measure_key VARCHAR(20) PRIMARY KEY, measure_code VARCHAR(10),
    measure_name VARCHAR(200), measure_type VARCHAR(50), star_weight FLOAT,
    age_gender_eligibility VARCHAR(200),
    clinical_description VARCHAR(500), nba_default_playbook VARCHAR(100)
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.DIM_PLAN_CONTRACT (
    plan_key VARCHAR(20) PRIMARY KEY, contract_id VARCHAR(20),
    plan_name VARCHAR(200), region VARCHAR(50), segment VARCHAR(50),
    star_rating_current FLOAT, star_rating_target FLOAT,
    plan_annual_revenue BIGINT, total_members INTEGER,
    plan_pmpm_monthly FLOAT, source_id VARCHAR(50)
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.DIM_MEMBER (
    member_key VARCHAR(30) PRIMARY KEY, dob_year INTEGER, age_band VARCHAR(10),
    gender VARCHAR(1), language_preference VARCHAR(50),
    digital_literacy_segment VARCHAR(20), socioeconomic_segment VARCHAR(20),
    display_name VARCHAR(200), source_id VARCHAR(50)
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.DIM_MEMBER_CHANNEL_PREF (
    member_key VARCHAR(30) PRIMARY KEY, email_allowed VARCHAR(5),
    sms_allowed VARCHAR(5), call_allowed VARCHAR(5),
    preferred_channel VARCHAR(20), do_not_contact_flag VARCHAR(5),
    channel_risk_notes VARCHAR(500)
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.FACT_MEMBER_GAP (
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
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.FACT_NBA_CLAUDE_DECISION (
    nba_run_id VARCHAR(30), member_gap_key VARCHAR(60),
    plan_key VARCHAR(20), measure_key VARCHAR(20), nba_action_type VARCHAR(50),
    cohort_id VARCHAR(30), cohort_name VARCHAR(100), cohort_priority_rank INTEGER,
    final_channel VARCHAR(20), final_incentive VARCHAR(30), priority_score FLOAT,
    sla_days_to_contact INTEGER, expected_gap_closure_lift FLOAT,
    reason_codes VARCHAR(500), explanation_text VARCHAR(2000),
    is_in_selected_opportunity INTEGER, created_at TIMESTAMP_NTZ,
    PRIMARY KEY (nba_run_id, member_gap_key)
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.DIM_NBA_CAMPAIGN (
    campaign_id VARCHAR(30) PRIMARY KEY, nba_run_id VARCHAR(30),
    plan_key VARCHAR(20), measure_key VARCHAR(20),
    channel_strategy VARCHAR(500), frequency_plan VARCHAR(500),
    incentive_strategy VARCHAR(500), message_template VARCHAR(2000),
    target_cohort_ids VARCHAR(500), created_at TIMESTAMP_NTZ
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.FACT_NBA_OUTREACH_PLAN (
    outreach_id VARCHAR(60) PRIMARY KEY, nba_run_id VARCHAR(30),
    campaign_id VARCHAR(30), member_gap_key VARCHAR(60), channel VARCHAR(20),
    planned_datetime TIMESTAMP_NTZ, message_template VARCHAR(2000),
    incentive_offered VARCHAR(30), status VARCHAR(20), created_at TIMESTAMP_NTZ
)""")

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {NEW_DB}.{SCHEMA}.FACT_NBA_TRACE (
    trace_id VARCHAR(60) PRIMARY KEY, nba_run_id VARCHAR(30),
    agent_step VARCHAR(50), agent_mode VARCHAR(50),
    input_summary VARCHAR(2000), output_summary VARCHAR(2000),
    affected_population_count INTEGER, created_at TIMESTAMP_NTZ
)""")

raw_conn._conn.commit()
print("Tables created.")

# ── Seed measures ─────────────────────────────────────────────────────────────
print("Seeding measures...")
cur.executemany(f"""
INSERT INTO {NEW_DB}.{SCHEMA}.DIM_MEASURE
(measure_key,measure_code,measure_name,measure_type,star_weight,age_gender_eligibility,clinical_description,nba_default_playbook)
VALUES (%s,%s,%s,%s,%s,%s,%s,'STANDARD')
""", [(m[0],m[1],m[2],m[3],m[4],m[5],m[6]) for m in MEASURES])
raw_conn._conn.commit()

# ── Seed plans ────────────────────────────────────────────────────────────────
print("Seeding plans...")
cur.executemany(f"""
INSERT INTO {NEW_DB}.{SCHEMA}.DIM_PLAN_CONTRACT
(plan_key,contract_id,plan_name,region,segment,star_rating_current,star_rating_target,plan_annual_revenue,total_members,plan_pmpm_monthly,source_id)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'meridian_sf')
""", [(p[0],p[1],p[2],p[3],p[4],p[5],p[6],p[7],p[8],p[9]) for p in PLANS])
raw_conn._conn.commit()

# ── Seed members and gaps ─────────────────────────────────────────────────────
# ~600 members per plan = ~4200 total members
# Each member gets gaps for 4-6 measures = ~20,000+ gaps
print("Generating members and gaps (this may take a minute)...")
MEMBERS_PER_PLAN = 600
all_members = []
for plan in PLANS:
    plan_key = plan[0]
    for i in range(MEMBERS_PER_PLAN):
        all_members.append(gen_member(len(all_members), plan_key))

print(f"  {len(all_members)} members generated")

# Batch insert members
BATCH = 500
for start in range(0, len(all_members), BATCH):
    batch = all_members[start:start+BATCH]
    cur.executemany(f"""
    INSERT INTO {NEW_DB}.{SCHEMA}.DIM_MEMBER
    (member_key,dob_year,age_band,gender,language_preference,digital_literacy_segment,socioeconomic_segment,display_name,source_id)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'meridian_sf')
    """, [(m["member_key"],m["dob_year"],m["age_band"],m["gender"],m["language_preference"],
           m["digital_literacy_segment"],m["socioeconomic_segment"],m["display_name"]) for m in batch])
    cur.executemany(f"""
    INSERT INTO {NEW_DB}.{SCHEMA}.DIM_MEMBER_CHANNEL_PREF
    (member_key,email_allowed,sms_allowed,call_allowed,preferred_channel,do_not_contact_flag)
    VALUES (%s,%s,%s,%s,%s,%s)
    """, [(m["member_key"],m["email_allowed"],m["sms_allowed"],m["call_allowed"],
           m["preferred_channel"],m["do_not_contact_flag"]) for m in batch])
    raw_conn._conn.commit()
    print(f"  Members {start+len(batch)}/{len(all_members)} inserted")

# Generate gaps
print("Generating gaps...")
measure_pool = MEASURES  # all 10 measures
all_gaps = []
for m in all_members:
    # Each member gets 4-7 measures assigned
    n_measures = random.randint(4, 7)
    assigned = random.sample(measure_pool, n_measures)
    for meas in assigned:
        gap = gen_gap(m, meas[0], meas[1])
        all_gaps.append(gap)

print(f"  {len(all_gaps)} gaps generated")

for start in range(0, len(all_gaps), BATCH):
    batch = all_gaps[start:start+BATCH]
    cur.executemany(f"""
    INSERT INTO {NEW_DB}.{SCHEMA}.FACT_MEMBER_GAP
    (member_gap_key,member_key,measure_key,measure_code,plan_key,measurement_year,
     gap_status,gap_open_date,gap_close_date,days_open,clinical_risk_score,
     nba_propensity_score,previous_year_gap_flag,upstream_recommended_channel,
     upstream_recommended_incentive,upstream_recommended_priority,
     last_outreach_date,last_outreach_channel,is_suppressed,source_id)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, [(g["member_gap_key"],g["member_key"],g["measure_key"],g["measure_code"],
           g["plan_key"],g["measurement_year"],g["gap_status"],g["gap_open_date"],
           g["gap_close_date"],g["days_open"],g["clinical_risk_score"],
           g["nba_propensity_score"],g["previous_year_gap_flag"],
           g["upstream_recommended_channel"],g["upstream_recommended_incentive"],
           g["upstream_recommended_priority"],g["last_outreach_date"],
           g["last_outreach_channel"],g["is_suppressed"],g["source_id"]) for g in batch])
    raw_conn._conn.commit()
    if (start // BATCH) % 5 == 0:
        print(f"  Gaps {start+len(batch)}/{len(all_gaps)} inserted")

raw_conn._conn.commit()
raw_conn.close()

open_gaps = sum(1 for g in all_gaps if g["gap_status"] == "OPEN")
print(f"\n✅ Done! {NEW_DB}.{SCHEMA} created with:")
print(f"   Plans:   {len(PLANS)}")
print(f"   Members: {len(all_members)}")
print(f"   Gaps:    {len(all_gaps)} total / {open_gaps} open")
print(f"\nNow add it in the dashboard:")
print(f"   Label:     Meridian Health (Stars)")
print(f"   Database:  {NEW_DB}")
print(f"   Schema:    {SCHEMA}")
print(f"   Warehouse: {WAREHOUSE}")
