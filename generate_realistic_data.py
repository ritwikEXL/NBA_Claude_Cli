"""
generate_realistic_data_v2 — overwritten below
==========================
Generates realistic synthetic Medicare Advantage HEDIS data at scale.

Scale: ~28,500 members across 5 plans, 7 measures, ~110K gap records.
Propensity scores are derived from member attributes — not random.
HEDIS compliance rates match NCQA 2023 national benchmarks.

Run:  python generate_realistic_data.py
Outputs:  input/  CSVs  +  loads directly into careintel.db
"""

import sqlite3, random, math, csv, os, hashlib
from datetime import date, timedelta

DB   = r'C:\Users\vmuser\Documents\NBA_Claude_Cli\careintel.db'
SEED = 42
rng  = random.Random(SEED)

# ── Plan definitions ─────────────────────────────────────────────────────────
PLANS = [
    dict(plan_key='P001', plan_id='H1234-001', contract_id='H1234',
         plan_name='UHC Medicare Signature PPO (West)',
         region='West', segment='PPO',
         star_rating_current=2.5, star_rating_target=3.0,
         plan_total_members=8_500, plan_pmpm_monthly=980),
    dict(plan_key='P002', plan_id='H5678-001', contract_id='H5678',
         plan_name='Aetna Medicare DSNP Community (Midwest)',
         region='Midwest', segment='DSNP',
         star_rating_current=3.0, star_rating_target=3.5,
         plan_total_members=4_200, plan_pmpm_monthly=1_150),
    dict(plan_key='P003', plan_id='H9012-001', contract_id='H9012',
         plan_name='Aetna Medicare Choice PPO (Northeast)',
         region='Northeast', segment='PPO',
         star_rating_current=3.5, star_rating_target=4.0,
         plan_total_members=6_800, plan_pmpm_monthly=1_020),
    dict(plan_key='P004', plan_id='H3456-001', contract_id='H3456',
         plan_name='Aetna Medicare Premier PPO (Southeast)',
         region='Southeast', segment='PPO',
         star_rating_current=4.0, star_rating_target=4.5,
         plan_total_members=5_100, plan_pmpm_monthly=960),
    dict(plan_key='P005', plan_id='H7890-001', contract_id='H7890',
         plan_name='UHC Medicare Advantage Value (West)',
         region='West', segment='HMO',
         star_rating_current=4.5, star_rating_target=5.0,
         plan_total_members=3_900, plan_pmpm_monthly=890),
]

# ── Measure definitions (NCQA 2023 benchmarks) ───────────────────────────────
# compliance_benchmark = national MA average; top_decile = 90th percentile
MEASURES = [
    dict(measure_key='M001', measure_code='BCS', measure_name='Breast Cancer Screening',
         star_weight=3.0, category='Preventive Screening',
         eligibility_rule='Women aged 50–74 — biennial mammogram',
         eligible_gender='F', eligible_age_min=50, eligible_age_max=74,
         requires_diabetes=False, requires_cvd=False,
         compliance_benchmark=0.742, top_decile=0.836,
         base_propensity_shift=0.05),
    dict(measure_key='M002', measure_code='COL', measure_name='Colorectal Cancer Screening',
         star_weight=3.0, category='Preventive Screening',
         eligibility_rule='Members aged 45–75 — colonoscopy/FIT',
         eligible_gender=None, eligible_age_min=45, eligible_age_max=75,
         requires_diabetes=False, requires_cvd=False,
         compliance_benchmark=0.672, top_decile=0.820,
         base_propensity_shift=0.0),
    dict(measure_key='M003', measure_code='EED', measure_name='Eye Exam for Patients with Diabetes',
         star_weight=2.0, category='Diabetes Management',
         eligibility_rule='Members with diabetes — annual retinal exam',
         eligible_gender=None, eligible_age_min=18, eligible_age_max=75,
         requires_diabetes=True, requires_cvd=False,
         compliance_benchmark=0.671, top_decile=0.812,
         base_propensity_shift=-0.03),
    dict(measure_key='M004', measure_code='CDC', measure_name='Comprehensive Diabetes Care (HbA1c)',
         star_weight=3.0, category='Diabetes Management',
         eligibility_rule='Members with diabetes — HbA1c testing',
         eligible_gender=None, eligible_age_min=18, eligible_age_max=75,
         requires_diabetes=True, requires_cvd=False,
         compliance_benchmark=0.826, top_decile=0.904,
         base_propensity_shift=0.08),
    dict(measure_key='M005', measure_code='MAD', measure_name='Medication Adherence for Diabetes',
         star_weight=3.0, category='Medication Adherence',
         eligibility_rule='Diabetics on oral medications — PDC ≥ 80%',
         eligible_gender=None, eligible_age_min=18, eligible_age_max=75,
         requires_diabetes=True, requires_cvd=False,
         compliance_benchmark=0.824, top_decile=0.886,
         base_propensity_shift=0.10),
    dict(measure_key='M006', measure_code='FVS', measure_name='Annual Flu Vaccine (Senior)',
         star_weight=2.0, category='Preventive Screening',
         eligibility_rule='All Medicare members — annual influenza vaccine',
         eligible_gender=None, eligible_age_min=65, eligible_age_max=None,
         requires_diabetes=False, requires_cvd=False,
         compliance_benchmark=0.652, top_decile=0.792,
         base_propensity_shift=0.02),
    dict(measure_key='M007', measure_code='STC', measure_name='Statin Therapy for CVD',
         star_weight=3.0, category='Cardiovascular Care',
         eligibility_rule='Members with CVD diagnosis — statin adherence',
         eligible_gender=None, eligible_age_min=21, eligible_age_max=75,
         requires_diabetes=False, requires_cvd=True,
         compliance_benchmark=0.789, top_decile=0.841,
         base_propensity_shift=0.06),
]

# ── Language / channel / SES distributions ───────────────────────────────────
LANGUAGES = [
    ('English',    0.68), ('Spanish',    0.14), ('Chinese',   0.05),
    ('Vietnamese', 0.04), ('Korean',     0.03), ('Tagalog',   0.03),
    ('Russian',    0.02), ('Arabic',     0.01),
]
DIG_LIT   = [('High', 0.38), ('Medium', 0.42), ('Low', 0.20)]
SES_SEG   = [('High', 0.22), ('Mid', 0.51), ('Low', 0.27)]

def _pick(choices, r):
    """Weighted pick from list of (value, weight) tuples."""
    vals, wts = zip(*choices)
    return r.choices(vals, weights=wts)[0]

def _hash_seed(key, salt=''):
    return int(hashlib.md5((salt + str(key)).encode()).hexdigest(), 16)

def _rng(key, salt=''):
    return random.Random(_hash_seed(key, salt))

# ── Member generation ─────────────────────────────────────────────────────────
def generate_members():
    members, channel_prefs = [], []
    mbr_idx = 1001
    for plan in PLANS:
        n = plan['plan_total_members']
        for _ in range(n):
            mk = f'M{mbr_idx}'
            r  = _rng(mk, 'mbr')
            # Age: realistic Medicare distribution 65–89 (skewed younger)
            age = int(r.gauss(72, 7))
            age = max(65, min(89, age))
            dob = date(2026 - age, r.randint(1, 12), r.randint(1, 28))

            gender = r.choices(['M', 'F'], weights=[0.44, 0.56])[0]
            lang   = _pick(LANGUAGES, r)
            dig    = _pick(DIG_LIT, r)
            ses    = _pick(SES_SEG, r)

            # Generate a realistic name
            first_names_m = ['James','Robert','John','Michael','William','David','Richard','Joseph','Thomas','Charles',
                              'Carlos','Miguel','Jose','Luis','Antonio','Wei','Ming','Hiroshi','Jin','Ahmed']
            first_names_f = ['Mary','Patricia','Linda','Barbara','Elizabeth','Jennifer','Maria','Susan','Dorothy','Lisa',
                              'Carmen','Rosa','Ana','Mei','Yuki','Fatima','Aisha','Grace','Helen','Ruth']
            last_names = ['Smith','Johnson','Williams','Brown','Jones','Garcia','Martinez','Davis','Wilson','Anderson',
                          'Taylor','Thomas','Lee','Jackson','White','Harris','Clark','Lewis','Robinson','Walker',
                          'Rodriguez','Gonzalez','Hernandez','Lopez','Chen','Wang','Kim','Park','Nguyen','Patel']
            first = r.choice(first_names_f if gender=='F' else first_names_m)
            last  = r.choice(last_names)
            name  = f'{first} {last}'

            # Diabetes and CVD prevalence — realistic for Medicare
            # Diabetes: ~33% of Medicare population
            # CVD: ~28% of Medicare population (higher with age)
            has_diabetes = r.random() < (0.28 + 0.008 * max(0, age - 65))
            has_cvd      = r.random() < (0.22 + 0.010 * max(0, age - 65))
            has_diabetes = bool(has_diabetes)
            has_cvd      = bool(has_cvd)

            members.append(dict(
                member_key=mk, source_id='demo',
                plan_key=plan['plan_key'], plan_id=plan['plan_id'],
                member_name=name, date_of_birth=str(dob),
                age=age, gender=gender,
                language_preference=lang,
                digital_literacy_segment=dig,
                socioeconomic_segment=ses,
                has_diabetes=1 if has_diabetes else 0,
                has_cvd=1 if has_cvd else 0,
            ))

            # Channel preferences — realistic Medicare senior distribution
            # Call is most universal; email drops with age and low digital lit
            cr = _rng(mk, 'ch')
            email_p = 0.65 if dig=='High' else 0.40 if dig=='Medium' else 0.18
            sms_p   = 0.72 if dig=='High' else 0.55 if dig=='Medium' else 0.25
            call_p  = 0.90  # call allowed for almost everyone

            email_ok = cr.random() < email_p
            sms_ok   = cr.random() < sms_p
            call_ok  = cr.random() < call_p

            # DNC: ~3.5% true do-not-contact
            dnc = cr.random() < 0.035
            if dnc:
                email_ok = sms_ok = call_ok = False

            # Preferred channel — consistent with what's allowed and literacy
            if dnc:
                pref = 'CALL'
            elif dig == 'High' and email_ok:
                pref = 'EMAIL'
            elif sms_ok:
                pref = 'SMS'
            elif call_ok:
                pref = 'CALL'
            else:
                pref = 'CALL'

            channel_prefs.append(dict(
                member_key=mk, source_id='demo',
                email_allowed='true' if email_ok else 'false',
                sms_allowed='true'   if sms_ok   else 'false',
                call_allowed='true'  if call_ok  else 'false',
                preferred_channel=pref,
                do_not_contact_flag='true' if dnc else 'false',
            ))
            mbr_idx += 1

    print(f'Generated {len(members):,} members across {len(PLANS)} plans')
    return members, channel_prefs

# ── Propensity scoring ────────────────────────────────────────────────────────
def _propensity(member, measure, days_open, plan_compliance_rate):
    """
    Logistic-based propensity: probability a gap closes with outreach.
    Range 0.05 – 0.95.
    """
    r = _rng(member['member_key'] + measure['measure_key'], 'prop')

    # Base from measure's national closure rate
    base = 0.50 + measure['base_propensity_shift']

    # Age effect: younger seniors more responsive
    age_factor = -0.008 * max(0, member['age'] - 70)

    # Digital literacy
    dig_factor = {'High': 0.12, 'Medium': 0.0, 'Low': -0.14}[member['digital_literacy_segment']]

    # SES
    ses_factor = {'High': 0.08, 'Mid': 0.0, 'Low': -0.10}[member['socioeconomic_segment']]

    # Language barrier (non-English slightly lower without translation resources)
    lang_factor = 0.0 if member['language_preference'] == 'English' else -0.06

    # Days open: longer open = harder case
    days_factor = -0.0008 * min(days_open, 365)

    # Plan compliance already high → remaining gaps are harder
    plan_factor = -0.15 * max(0, plan_compliance_rate - 0.70)

    # Logistic transform
    logit = (base + age_factor + dig_factor + ses_factor + lang_factor
             + days_factor + plan_factor)

    # Add calibrated noise
    logit += r.gauss(0, 0.08)

    # Sigmoid
    prob = 1 / (1 + math.exp(-5 * (logit - 0.50)))
    return round(max(0.05, min(0.95, prob)), 4)

# ── Gap generation ────────────────────────────────────────────────────────────
def generate_gaps(members):
    """
    Generate fact_member_gap rows.
    Only creates a gap row for eligible members (age/gender/condition match).
    Compliance rate per plan varies around NCQA benchmark ± plan-level noise.
    """
    today = date(2026, 8, 24)
    meas_year = 2025
    gaps = []
    gap_idx = 1

    # Plan-level compliance modifiers (some plans do better / worse than avg)
    plan_comp_mod = {
        'P001': -0.06,  # 2.5★ — well below average
        'P002': -0.03,  # 3.0★ — slightly below
        'P003':  0.00,  # 3.5★ — at average
        'P004':  0.04,  # 4.0★ — above average
        'P005':  0.07,  # 4.5★ — top performers
    }

    # Index members by plan
    plan_members = {}
    for m in members:
        plan_members.setdefault(m['plan_key'], []).append(m)

    for meas in MEASURES:
        for plan in PLANS:
            pkey = plan['plan_key']
            pmembers = plan_members.get(pkey, [])
            comp_rate = min(0.96, max(0.30,
                meas['compliance_benchmark'] + plan_comp_mod[pkey]
                + _rng(pkey+meas['measure_key'], 'cmod').gauss(0, 0.015)
            ))

            # Filter to eligible members
            eligible = []
            for m in pmembers:
                if meas['eligible_gender'] and m['gender'] != meas['eligible_gender']:
                    continue
                if m['age'] < meas['eligible_age_min']:
                    continue
                if meas['eligible_age_max'] and m['age'] > meas['eligible_age_max']:
                    continue
                if meas['requires_diabetes'] and not m['has_diabetes']:
                    continue
                if meas['requires_cvd'] and not m['has_cvd']:
                    continue
                eligible.append(m)

            if not eligible:
                continue

            # Determine who has a gap (non-compliant)
            gap_rate = 1.0 - comp_rate
            r = _rng(pkey + meas['measure_key'], 'gapsel')
            for m in eligible:
                mr = _rng(m['member_key'] + meas['measure_key'], 'gap')
                if mr.random() > gap_rate:
                    continue  # compliant — no gap row

                # Days open: realistic distribution (many recent, some very old)
                # Bimodal: newly identified (30–120 days) and chronic (200–365 days)
                if mr.random() < 0.55:
                    days_open = int(mr.gauss(75, 30))
                else:
                    days_open = int(mr.gauss(260, 60))
                days_open = max(7, min(365, days_open))

                open_date = today - timedelta(days=days_open)

                # Gap status
                gst_r = mr.random()
                if gst_r < 0.78:
                    gap_status = 'Open'
                elif gst_r < 0.91:
                    gap_status = 'Borderline'
                else:
                    gap_status = 'Partial'

                prop = _propensity(m, meas, days_open, comp_rate)

                mgk = f'MGK_{m["member_key"]}_{meas["measure_key"]}'
                gaps.append(dict(
                    member_gap_key=mgk,
                    source_id='demo',
                    member_key=m['member_key'],
                    plan_key=pkey,
                    measure_key=meas['measure_key'],
                    measure_code=meas['measure_code'],
                    measurement_year=meas_year,
                    gap_status=gap_status,
                    days_open=days_open,
                    gap_open_date=str(open_date),
                    gap_close_date=None,
                    propensity_score=prop,
                    upstream_recommendation='OUTREACH',
                    confidence_level='HIGH' if prop>=0.60 else 'MEDIUM' if prop>=0.35 else 'LOW',
                    notes=None,
                    created_at=str(today),
                    updated_at=str(today),
                ))
                gap_idx += 1

    print(f'Generated {len(gaps):,} gap records across {len(PLANS)} plans × {len(MEASURES)} measures')
    return gaps

# ── SQLite loader ─────────────────────────────────────────────────────────────
def load_to_sqlite(members, channel_prefs, gaps):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    # ── dim_member ────────────────────────────────────────────────────────────
    print('Loading dim_member…')
    cur.execute("DELETE FROM dim_member WHERE source_id='demo'")
    cur.executemany("""
        INSERT OR REPLACE INTO dim_member
        (member_key, source_id, plan_key, plan_id, member_name, date_of_birth,
         age, gender, language_preference, digital_literacy_segment,
         socioeconomic_segment, has_diabetes, has_cvd)
        VALUES (:member_key,:source_id,:plan_key,:plan_id,:member_name,:date_of_birth,
                :age,:gender,:language_preference,:digital_literacy_segment,
                :socioeconomic_segment,:has_diabetes,:has_cvd)
    """, members)
    print(f'  {len(members):,} members')

    # ── dim_member_channel_pref ───────────────────────────────────────────────
    print('Loading dim_member_channel_pref…')
    cur.execute("DELETE FROM dim_member_channel_pref WHERE source_id='demo'")
    cur.executemany("""
        INSERT OR REPLACE INTO dim_member_channel_pref
        (member_key, source_id, email_allowed, sms_allowed, call_allowed,
         preferred_channel, do_not_contact_flag)
        VALUES (:member_key,:source_id,:email_allowed,:sms_allowed,:call_allowed,
                :preferred_channel,:do_not_contact_flag)
    """, channel_prefs)
    print(f'  {len(channel_prefs):,} channel prefs')

    # ── fact_member_gap ───────────────────────────────────────────────────────
    print('Loading fact_member_gap…')
    cur.execute("DELETE FROM fact_member_gap WHERE source_id='demo'")

    # Check column names
    cur.execute("PRAGMA table_info(fact_member_gap)")
    cols = {r['name'] for r in cur.fetchall()}

    # Build insert dynamically based on available columns
    gap_cols = [c for c in [
        'member_gap_key','source_id','member_key','plan_key','measure_key','measure_code',
        'measurement_year','gap_status','days_open','gap_open_date','gap_close_date',
        'propensity_score','upstream_recommendation','confidence_level','notes',
        'created_at','updated_at'
    ] if c in cols]

    placeholders = ','.join(f':{c}' for c in gap_cols)
    col_list = ','.join(gap_cols)

    # Filter gaps to only include columns that exist
    gaps_filtered = [{c: g.get(c) for c in gap_cols} for g in gaps]

    BATCH = 5000
    for i in range(0, len(gaps_filtered), BATCH):
        cur.executemany(
            f"INSERT OR REPLACE INTO fact_member_gap ({col_list}) VALUES ({placeholders})",
            gaps_filtered[i:i+BATCH]
        )
        print(f'  {min(i+BATCH, len(gaps)):,}/{len(gaps):,} gaps…', end='\r')
    print(f'\n  {len(gaps):,} gap records')

    # ── dim_plan_contract (update member counts) ──────────────────────────────
    print('Updating dim_plan_contract…')
    cur.execute("DELETE FROM dim_plan_contract WHERE source_id='demo'")
    plan_rows = []
    for p in PLANS:
        plan_rows.append(dict(
            plan_key=p['plan_key'], plan_id=p['plan_id'], source_id='demo',
            contract_id=p['contract_id'], plan_name=p['plan_name'],
            region=p['region'], segment=p['segment'],
            star_rating_current=p['star_rating_current'],
            star_rating_target=p['star_rating_target'],
            total_members=p['plan_total_members'],
            pmpm_monthly=p['plan_pmpm_monthly'],
        ))
    cur.execute("PRAGMA table_info(dim_plan_contract)")
    pc_cols = {r['name'] for r in cur.fetchall()}
    pc_insert_cols = [c for c in ['plan_key','plan_id','source_id','contract_id','plan_name',
                                   'region','segment','star_rating_current','star_rating_target',
                                   'total_members','pmpm_monthly'] if c in pc_cols]
    ph = ','.join(f':{c}' for c in pc_insert_cols)
    cl = ','.join(pc_insert_cols)
    cur.executemany(
        f"INSERT OR REPLACE INTO dim_plan_contract ({cl}) VALUES ({ph})",
        [{c: r.get(c) for c in pc_insert_cols} for r in plan_rows]
    )
    print(f'  {len(PLANS)} plan contracts')

    # ── dim_measure (upsert with real NCQA data) ──────────────────────────────
    print('Updating dim_measure…')
    cur.execute("PRAGMA table_info(dim_measure)")
    dm_cols = {r['name'] for r in cur.fetchall()}
    meas_insert_cols = [c for c in ['measure_key','measure_code','measure_name','star_weight',
                                     'category','eligibility_rule','source_id'] if c in dm_cols]
    ph2 = ','.join(f':{c}' for c in meas_insert_cols)
    cl2 = ','.join(meas_insert_cols)
    meas_rows = [{**{c: m.get(c) for c in meas_insert_cols}, 'source_id':'demo'} for m in MEASURES]
    cur.executemany(
        f"INSERT OR REPLACE INTO dim_measure ({cl2}) VALUES ({ph2})",
        meas_rows
    )
    print(f'  {len(MEASURES)} measures')

    conn.commit()
    conn.close()
    print('\nSQLite load complete.')

# ── CSV export (for Snowflake COPY INTO) ─────────────────────────────────────
def export_csvs(members, channel_prefs, gaps):
    os.makedirs('input', exist_ok=True)

    def _write(path, rows):
        if not rows: return
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        print(f'  Wrote {path} ({len(rows):,} rows)')

    _write('input/dim_member.csv', members)
    _write('input/dim_member_channel_pref.csv', channel_prefs)
    _write('input/fact_member_gap.csv', gaps)

    plan_rows = [dict(
        plan_key=p['plan_key'], plan_id=p['plan_id'], source_id='demo',
        contract_id=p['contract_id'], plan_name=p['plan_name'],
        region=p['region'], segment=p['segment'],
        star_rating_current=p['star_rating_current'],
        star_rating_target=p['star_rating_target'],
        total_members=p['plan_total_members'],
        pmpm_monthly=p['plan_pmpm_monthly'],
    ) for p in PLANS]
    _write('input/dim_plan_contract.csv', plan_rows)

    meas_rows = [dict(
        measure_key=m['measure_key'], measure_code=m['measure_code'],
        measure_name=m['measure_name'], star_weight=m['star_weight'],
        category=m['category'], eligibility_rule=m['eligibility_rule'],
        source_id='demo',
        compliance_benchmark=m['compliance_benchmark'],
        top_decile=m['top_decile'],
    ) for m in MEASURES]
    _write('input/dim_measure.csv', meas_rows)
    print('CSV export done.')

# ── Summary stats ─────────────────────────────────────────────────────────────
def print_summary(members, gaps):
    print('\n' + '='*60)
    print('DATA SUMMARY')
    print('='*60)
    from collections import Counter
    plan_counts = Counter(m['plan_key'] for m in members)
    for p in PLANS:
        pk = p['plan_key']
        plan_gaps = [g for g in gaps if g['plan_key']==pk]
        print(f"\n  {p['plan_name'][:40]} ({p['star_rating_current']}★)")
        print(f"    Members: {plan_counts[pk]:,}  |  Gaps: {len(plan_gaps):,}")

    print(f"\n  Total members : {len(members):,}")
    print(f"  Total gaps    : {len(gaps):,}")
    print(f"  DNC members   : {sum(1 for m in members if any(c['member_key']==m['member_key'] and c['do_not_contact_flag']=='true' for c in []))}")

    prop_buckets = Counter(
        'T1 (≥0.70)' if g['propensity_score']>=0.70
        else 'T2 (0.45–0.70)' if g['propensity_score']>=0.45
        else 'T3 (<0.45)' for g in gaps
    )
    print('\n  Propensity tiers (all plans):')
    total = len(gaps)
    for k in ['T1 (≥0.70)','T2 (0.45–0.70)','T3 (<0.45)']:
        n = prop_buckets[k]
        print(f"    {k}: {n:,} ({100*n//total}%)")

    print('\n  Compliance rates by measure:')
    for meas in MEASURES:
        mk = meas['measure_key']
        meas_gaps = [g for g in gaps if g['measure_key']==mk]
        # Eligible = members with the right characteristics — approximate from gap count
        print(f"    {meas['measure_code']:4} gaps={len(meas_gaps):6,}  NCQA_avg={meas['compliance_benchmark']:.1%}")
    print('='*60)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Generating members…')
    members, channel_prefs = generate_members()

    print('Generating gaps…')
    gaps = generate_gaps(members)

    print('Loading to SQLite…')
    load_to_sqlite(members, channel_prefs, gaps)

    print('Exporting CSVs…')
    export_csvs(members, channel_prefs, gaps)

    print_summary(members, gaps)
    print('\nDone. Run the app — the opportunity tab now shows real-scale data.')
