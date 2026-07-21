#!/usr/bin/env python3
"""
Generate scaled synthetic data for CareIntel demo.
Target: ~6,000 members across 5 plans, realistic eligibility rates per measure,
plan-specific compliance variance tied to Star ratings.
Writes to input/ CSVs, then calls setup_database.py to reload the DB.
"""
import csv, os, random, sqlite3
from datetime import date, timedelta

random.seed(2026)

BASE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(BASE, "input")
DB    = os.path.join(BASE, "careintel.db")

# ── Plan definitions ──────────────────────────────────────────────────────────
PLANS = [
    {"plan_key":"P001","contract_id":"H1234","plan_name":"Aurora MA-PD Choice",
     "region":"Northeast","segment":"MAPD","star_rating_current":3.5,"star_rating_target":4.0,
     "total_members":1500,"plan_pmpm_monthly":1050},
    {"plan_key":"P002","contract_id":"H2345","plan_name":"Aurora MA-PD Premier",
     "region":"Southeast","segment":"MAPD","star_rating_current":4.0,"star_rating_target":4.5,
     "total_members":1200,"plan_pmpm_monthly":1120},
    {"plan_key":"P003","contract_id":"H3456","plan_name":"Aurora DSNP Community",
     "region":"Midwest","segment":"DSNP","star_rating_current":3.0,"star_rating_target":4.0,
     "total_members":1000,"plan_pmpm_monthly":980},
    {"plan_key":"P004","contract_id":"H4567","plan_name":"Aurora MA Value",
     "region":"West","segment":"MA-only","star_rating_current":4.5,"star_rating_target":5.0,
     "total_members":1800,"plan_pmpm_monthly":1180},
    {"plan_key":"P005","contract_id":"H5678","plan_name":"Aurora MA-PD Signature",
     "region":"West","segment":"MAPD","star_rating_current":2.5,"star_rating_target":3.0,
     "total_members":500,"plan_pmpm_monthly":920},
]

# ── Measure definitions (unchanged) ──────────────────────────────────────────
MEASURES = [
    {"measure_key":"M001","measure_code":"BCS","measure_name":"Breast Cancer Screening",
     "measure_type":"Process","star_weight":1,"hedis_domain":"Effectiveness of Care - Prevention and Screening",
     "age_gender_eligibility":"Women aged 50-74","clinical_description":"Mammogram screening in past 2 years",
     "nba_default_playbook":"PB_BCS_STANDARD"},
    {"measure_key":"M002","measure_code":"COL","measure_name":"Colorectal Cancer Screening",
     "measure_type":"Process","star_weight":1.5,"hedis_domain":"Effectiveness of Care - Prevention and Screening",
     "age_gender_eligibility":"Adults aged 45-75","clinical_description":"Colorectal cancer screening",
     "nba_default_playbook":"PB_COL_STANDARD"},
    {"measure_key":"M003","measure_code":"EED","measure_name":"Eye Exam for Patients with Diabetes",
     "measure_type":"Process","star_weight":2,"hedis_domain":"Effectiveness of Care - Diabetes Care",
     "age_gender_eligibility":"Adults 18-75 with diabetes","clinical_description":"Retinal exam for diabetic members",
     "nba_default_playbook":"PB_EED_STANDARD"},
    {"measure_key":"M004","measure_code":"CDC","measure_name":"Controlling Blood Pressure",
     "measure_type":"Process","star_weight":2,"hedis_domain":"Effectiveness of Care - Chronic Conditions",
     "age_gender_eligibility":"Adults 18-85 with hypertension","clinical_description":"Blood pressure control",
     "nba_default_playbook":"PB_CDC_STANDARD"},
    {"measure_key":"M005","measure_code":"MAD","measure_name":"Medication Adherence for Diabetes",
     "measure_type":"Process","star_weight":3,"hedis_domain":"Effectiveness of Care - Medication Use",
     "age_gender_eligibility":"Adults with diabetes on medication","clinical_description":"Medication adherence for diabetes",
     "nba_default_playbook":"PB_MAD_STANDARD"},
    {"measure_key":"M006","measure_code":"AFV","measure_name":"Annual Flu Vaccine",
     "measure_type":"Process","star_weight":1,"hedis_domain":"Effectiveness of Care - Prevention and Screening",
     "age_gender_eligibility":"Adults 18+","clinical_description":"Annual flu vaccine",
     "nba_default_playbook":"PB_AFV_STANDARD"},
    {"measure_key":"M007","measure_code":"SPC","measure_name":"Statin Use in Persons with Cardiovascular Disease",
     "measure_type":"Process","star_weight":2,"hedis_domain":"Effectiveness of Care - Medication Use",
     "age_gender_eligibility":"Adults with cardiovascular disease","clinical_description":"Statin use for CVD",
     "nba_default_playbook":"PB_SPC_STANDARD"},
]

# ── Target compliance rates per measure × plan ────────────────────────────────
# Base rates by measure, then plan modifier based on Star rating
BASE_COMPLIANCE = {
    "BCS": 0.55, "COL": 0.45, "EED": 0.52,
    "CDC": 0.42, "MAD": 0.48, "AFV": 0.50, "SPC": 0.58,
}
# Modifier relative to base: higher-rated plans comply more
PLAN_MODIFIER = {
    "P004": +0.08,   # 4.5 Stars — best performer
    "P002": +0.05,   # 4.0 Stars
    "P001":  0.00,   # 3.5 Stars — baseline
    "P003": -0.06,   # 3.0 Stars
    "P005": -0.12,   # 2.5 Stars — worst performer
}

def compliance_rate(mc, pk):
    base = BASE_COMPLIANCE[mc]
    mod  = PLAN_MODIFIER.get(pk, 0)
    # Add small random noise per plan×measure so not perfectly smooth
    noise = random.gauss(0, 0.02)
    return max(0.10, min(0.90, base + mod + noise))

# ── Member attribute pools ────────────────────────────────────────────────────
LANGUAGES = ["EN"]*70 + ["ES"]*15 + ["ZH"]*5 + ["VI"]*4 + ["KO"]*3 + ["PL"]*3
DIG_LIT   = ["High"]*30 + ["Medium"]*45 + ["Low"]*25
SES_SEGS  = ["High"]*20 + ["Mid"]*55 + ["Low"]*25
GENDERS   = ["F", "M"]
CHANNELS  = ["EMAIL", "SMS", "CALL"]
PROVIDERS = [f"PRV{100+i}" for i in range(50)]

# DSNP members skew younger (Medicaid dual-eligible, 18-64), others are Medicare 65+
def make_age(segment):
    if segment == "DSNP":
        return random.randint(45, 74)   # Dual eligible, younger mix
    return random.randint(65, 87)       # Standard Medicare

def dob_year(age):
    return 2026 - age

def age_band(age):
    lo = (age // 5) * 5
    return f"{lo}-{lo+4}"

# ── Condition flags (used for measure eligibility) ────────────────────────────
# Each member gets permanent condition flags drawn at generation time
def assign_conditions():
    return {
        "has_diabetes":      random.random() < 0.18,
        "has_hypertension":  random.random() < 0.40,
        "has_cvd":           random.random() < 0.22,
    }

def is_eligible(member, measure_code):
    age = 2026 - member["dob_year"]
    g   = member["gender"]
    c   = member["conditions"]
    if measure_code == "BCS":
        return g == "F" and 50 <= age <= 74
    if measure_code == "COL":
        return 45 <= age <= 75
    if measure_code == "EED":
        return c["has_diabetes"] and 18 <= age <= 75
    if measure_code == "CDC":
        return c["has_hypertension"] and 18 <= age <= 85
    if measure_code == "MAD":
        return c["has_diabetes"]
    if measure_code == "AFV":
        return age >= 18
    if measure_code == "SPC":
        return c["has_cvd"]
    return False

# ── Generate members ──────────────────────────────────────────────────────────
print("Generating members...")
members = []
mbr_idx = 1
for plan in PLANS:
    seg = plan["segment"]
    for _ in range(plan["total_members"]):
        age = make_age(seg)
        g   = random.choice(GENDERS)
        lang = random.choice(LANGUAGES)
        members.append({
            "member_key": f"MBR{mbr_idx:05d}",
            "plan_key":   plan["plan_key"],
            "dob_year":   dob_year(age),
            "age":        age,
            "gender":     g,
            "language_preference": lang,
            "digital_literacy_segment": random.choice(DIG_LIT),
            "socioeconomic_segment":    random.choice(SES_SEGS),
            "pcp_provider_key": random.choice(PROVIDERS),
            "conditions": assign_conditions(),
        })
        mbr_idx += 1

print(f"  {len(members)} members generated across {len(PLANS)} plans")

# ── Write dim_member.csv ──────────────────────────────────────────────────────
member_fields = ["member_key","dob_year","age_band","gender","language_preference",
                 "digital_literacy_segment","socioeconomic_segment","pcp_provider_key"]
with open(os.path.join(INPUT, "dim_member.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=member_fields)
    w.writeheader()
    for m in members:
        w.writerow({
            "member_key": m["member_key"],
            "dob_year": m["dob_year"],
            "age_band": age_band(m["age"]),
            "gender": m["gender"],
            "language_preference": m["language_preference"],
            "digital_literacy_segment": m["digital_literacy_segment"],
            "socioeconomic_segment": m["socioeconomic_segment"],
            "pcp_provider_key": m["pcp_provider_key"],
        })
print(f"  dim_member.csv: {len(members)} rows")

# ── Write dim_member_channel_pref.csv ─────────────────────────────────────────
channel_notes = {
    "EMAIL": "Engaged via portal",
    "SMS": "Mobile preferred",
    "CALL": "Phone outreach preferred",
}
with open(os.path.join(INPUT, "dim_member_channel_pref.csv"), "w", newline="", encoding="utf-8") as f:
    fields = ["member_key","email_allowed","sms_allowed","call_allowed",
              "preferred_channel","do_not_contact_flag","channel_risk_notes"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for m in members:
        dnc = "true" if random.random() < 0.04 else "false"
        lang = m["language_preference"]
        # Non-English speakers slightly less likely to have email
        email_p = 0.55 if lang == "EN" else 0.35
        sms_p   = 0.80
        call_p  = 0.90
        pref = random.choice(CHANNELS)
        w.writerow({
            "member_key": m["member_key"],
            "email_allowed": "true" if random.random() < email_p else "false",
            "sms_allowed":   "true" if random.random() < sms_p   else "false",
            "call_allowed":  "true" if random.random() < call_p  else "false",
            "preferred_channel": pref,
            "do_not_contact_flag": dnc,
            "channel_risk_notes": channel_notes.get(pref, "Standard outreach"),
        })
print("  dim_member_channel_pref.csv written")

# ── Write dim_plan_contract.csv ───────────────────────────────────────────────
with open(os.path.join(INPUT, "dim_plan_contract.csv"), "w", newline="", encoding="utf-8") as f:
    fields = ["plan_key","contract_id","plan_name","region","segment",
              "star_rating_current","star_rating_target"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for p in PLANS:
        w.writerow({k: p[k] for k in fields})
print("  dim_plan_contract.csv written")

# ── Write dim_measure.csv ─────────────────────────────────────────────────────
with open(os.path.join(INPUT, "dim_measure.csv"), "w", newline="", encoding="utf-8") as f:
    fields = ["measure_key","measure_code","measure_name","measure_type","star_weight",
              "hedis_domain","age_gender_eligibility","clinical_description","nba_default_playbook"]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for m in MEASURES:
        w.writerow(m)
print("  dim_measure.csv written")

# ── Generate fact_member_gap.csv ──────────────────────────────────────────────
print("Generating gaps...")
CHANNELS_OUT = ["EMAIL","SMS","CALL","WHATSAPP"]
INCENTIVES   = ["GIFTCARD_15","GIFTCARD_25","TRANSPORT_VOUCHER","NONE"]
PRIORITIES   = ["High","Medium","Low"]

gap_rows = []
gap_idx  = 1
start_date = date(2026, 1, 15)
today      = date(2026, 7, 10)

measure_by_code = {m["measure_code"]: m for m in MEASURES}

for m in members:
    pk = m["plan_key"]
    for measure in MEASURES:
        mc  = measure["measure_code"]
        mk  = measure["measure_key"]
        if not is_eligible(m, mc):
            continue

        # Determine compliance for this member based on plan×measure target rate
        target = compliance_rate(mc, pk)
        is_closed = random.random() < target

        gap_open = start_date + timedelta(days=random.randint(0, 30))
        days_open = (today - gap_open).days if not is_closed else random.randint(10, 120)

        if is_closed:
            gap_status = "Closed"
            close_days = random.randint(10, days_open)
            gap_close  = gap_open + timedelta(days=close_days)
        else:
            # ~8% of open gaps are "Borderline" (close to compliant)
            gap_status = "Borderline" if random.random() < 0.08 else "Open"
            gap_close  = ""

        had_prior_gap   = "true" if random.random() < 0.45 else "false"
        last_outreach   = ""
        last_channel    = ""
        if not is_closed and random.random() < 0.40:
            last_outreach = str(start_date + timedelta(days=random.randint(5, 120)))
            last_channel  = random.choice(CHANNELS_OUT)

        gap_rows.append({
            "member_gap_key": f"G{gap_idx:05d}",
            "member_key": m["member_key"],
            "measure_key": mk,
            "measure_code": mc,
            "plan_key": pk,
            "measurement_year": 2026,
            "gap_status": gap_status,
            "gap_open_date": str(gap_open),
            "gap_close_date": str(gap_close) if gap_close else "",
            "days_open": days_open,
            "clinical_risk_score": round(random.uniform(0.20, 0.95), 2),
            "nba_propensity_score": 0.50,   # recalculated by setup_database.py
            "previous_year_gap_flag": had_prior_gap,
            "upstream_recommended_channel": random.choice(CHANNELS_OUT),
            "upstream_recommended_incentive": random.choice(INCENTIVES),
            "upstream_recommended_priority": random.choice(PRIORITIES),
            "last_outreach_date": last_outreach,
            "last_outreach_channel": last_channel,
            "is_suppressed": "true" if random.random() < 0.02 else "false",
        })
        gap_idx += 1

print(f"  {len(gap_rows)} gap rows generated")

gap_fields = [
    "member_gap_key","member_key","measure_key","measure_code","plan_key",
    "measurement_year","gap_status","gap_open_date","gap_close_date","days_open",
    "clinical_risk_score","nba_propensity_score","previous_year_gap_flag",
    "upstream_recommended_channel","upstream_recommended_incentive",
    "upstream_recommended_priority","last_outreach_date","last_outreach_channel",
    "is_suppressed",
]
with open(os.path.join(INPUT, "fact_member_gap.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=gap_fields)
    w.writeheader()
    w.writerows(gap_rows)
print("  fact_member_gap.csv written")

# ── Print preview stats ───────────────────────────────────────────────────────
print("\n--- Eligibility preview (rows in gap table per measure x plan) ---")
from collections import defaultdict
elig_count = defaultdict(int)
comp_count = defaultdict(int)
for g in gap_rows:
    key = (g["measure_code"], g["plan_key"])
    elig_count[key] += 1
    if g["gap_status"] == "Closed":
        comp_count[key] += 1

for mc in ["BCS","COL","EED","CDC","MAD","AFV","SPC"]:
    row = []
    for pk in ["P001","P002","P003","P004","P005"]:
        e = elig_count[(mc,pk)]
        c = comp_count[(mc,pk)]
        rate = round(c/e*100,0) if e else 0
        row.append(f"{pk}:{e}el/{c}cl({rate}%)")
    print(f"  {mc}: " + "  ".join(row))

print("\nDone. Now run setup_database.py to reload the DB.")
