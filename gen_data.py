"""
gen_data.py — Realistic Medicare HEDIS synthetic data generator
Matches careintel.db schema exactly (verified from PRAGMA table_info).

Scale: ~26K members, 5 plans, 7 measures, ~95K gap records.
Run:  python gen_data.py
"""
import sqlite3, random, math, csv, os, hashlib
from datetime import date, timedelta

DB   = r'C:\Users\vmuser\Documents\NBA_Claude_Cli\careintel.db'

PLANS = [
    dict(plan_key='P001', contract_id='H1234', plan_name='UHC Medicare Signature PPO (West)',
         region='West', segment='PPO', star_rating_current=2.5, star_rating_target=3.0, n=7_800, pmpm=980),
    dict(plan_key='P002', contract_id='H5678', plan_name='Aetna Medicare DSNP Community (Midwest)',
         region='Midwest', segment='DSNP', star_rating_current=3.0, star_rating_target=3.5, n=3_900, pmpm=1_150),
    dict(plan_key='P003', contract_id='H9012', plan_name='Aetna Medicare Choice PPO (Northeast)',
         region='Northeast', segment='PPO', star_rating_current=3.5, star_rating_target=4.0, n=6_200, pmpm=1_020),
    dict(plan_key='P004', contract_id='H3456', plan_name='Aetna Medicare Premier PPO (Southeast)',
         region='Southeast', segment='PPO', star_rating_current=4.0, star_rating_target=4.5, n=4_700, pmpm=960),
    dict(plan_key='P005', contract_id='H7890', plan_name='UHC Medicare Advantage Value (West)',
         region='West', segment='HMO', star_rating_current=4.5, star_rating_target=5.0, n=3_400, pmpm=890),
]

MEASURES = [
    dict(mk='M001', mc='BCS', name='Breast Cancer Screening',      sw=3.0, cat='Preventive',
         eg='F',  amin=50, amax=74, diab=False, cvd=False, avg=0.742, td=0.836, ps=0.05),
    dict(mk='M002', mc='COL', name='Colorectal Cancer Screening',  sw=3.0, cat='Preventive',
         eg=None, amin=45, amax=75, diab=False, cvd=False, avg=0.672, td=0.820, ps=0.00),
    dict(mk='M003', mc='EED', name='Eye Exam (Diabetes)',          sw=2.0, cat='Diabetes',
         eg=None, amin=18, amax=75, diab=True,  cvd=False, avg=0.671, td=0.812, ps=-0.03),
    dict(mk='M004', mc='CDC', name='Diabetes HbA1c Testing',       sw=3.0, cat='Diabetes',
         eg=None, amin=18, amax=75, diab=True,  cvd=False, avg=0.826, td=0.904, ps=0.08),
    dict(mk='M005', mc='MAD', name='Medication Adherence (Diab)',  sw=3.0, cat='Adherence',
         eg=None, amin=18, amax=75, diab=True,  cvd=False, avg=0.824, td=0.886, ps=0.10),
    dict(mk='M006', mc='FVS', name='Annual Flu Vaccine (Senior)',  sw=2.0, cat='Preventive',
         eg=None, amin=65, amax=None,diab=False, cvd=False, avg=0.652, td=0.792, ps=0.02),
    dict(mk='M007', mc='STC', name='Statin Therapy for CVD',      sw=3.0, cat='Cardiovascular',
         eg=None, amin=21, amax=75, diab=False, cvd=True,  avg=0.789, td=0.841, ps=0.06),
]

LANGS = [('English',0.68),('Spanish',0.14),('Chinese',0.05),('Vietnamese',0.04),
         ('Korean',0.03),('Tagalog',0.03),('Russian',0.02),('Arabic',0.01)]
DIGS  = [('High',0.38),('Medium',0.42),('Low',0.20)]
SESS  = [('High',0.22),('Mid',0.51),('Low',0.27)]

FF=['Mary','Patricia','Linda','Barbara','Elizabeth','Jennifer','Maria','Susan','Dorothy','Lisa',
    'Carmen','Rosa','Ana','Mei','Fatima','Grace','Helen','Ruth','Margaret','Sandra',
    'Ashley','Donna','Kimberly','Emily','Shirley','Emma','Joan','Evelyn','Olivia','Cynthia']
FM=['James','Robert','John','Michael','William','David','Richard','Joseph','Thomas','Charles',
    'Carlos','Miguel','Jose','Luis','Antonio','Wei','Ahmed','Jin','George','Kenneth',
    'Steven','Edward','Brian','Ronald','Anthony','Kevin','Jason','Matthew','Gary','Frank']
FN=['Smith','Johnson','Williams','Brown','Jones','Garcia','Martinez','Davis','Wilson','Anderson',
    'Taylor','Thomas','Lee','Jackson','White','Harris','Clark','Lewis','Robinson','Walker',
    'Rodriguez','Gonzalez','Hernandez','Lopez','Chen','Wang','Kim','Park','Nguyen','Patel',
    'Campbell','Mitchell','Perez','Roberts','Turner','Phillips','Evans','Torres','Collins','Ramirez']

def rng(key):
    return random.Random(int(hashlib.md5(str(key).encode()).hexdigest(),16))

def wt(lst, r):
    v,w=zip(*lst); return r.choices(v,weights=w)[0]

BANDS=['65-69','70-74','75-79','80-84','85-89']
BWS  =[0.35,0.28,0.20,0.11,0.06]

def gen_members():
    mems, chans, ext = [], [], []
    idx = 10001
    for p in PLANS:
        for _ in range(p['n']):
            mk = f'M{idx}'; idx+=1
            r  = rng(mk+'m')
            g  = r.choices(['M','F'],weights=[0.44,0.56])[0]
            ab = r.choices(BANDS,weights=BWS)[0]
            age= int(ab.split('-')[0]) + r.randint(0,4)
            dy = 2026-age
            la = wt(LANGS,r); di=wt(DIGS,r); se=wt(SESS,r)
            nm = r.choice(FF if g=='F' else FM)+' '+r.choice(FN)
            diab = r.random() < min(0.60, 0.28+0.007*max(0,age-65))
            cvd  = r.random() < min(0.55, 0.22+0.009*max(0,age-65))

            mems.append(dict(member_key=mk,dob_year=dy,age_band=ab,gender=g,
                             language_preference=la,digital_literacy_segment=di,
                             socioeconomic_segment=se,pcp_provider_key=f'PCP{r.randint(100,999)}',
                             display_name=nm,source_id='demo'))

            cr=rng(mk+'c')
            eo=cr.random()<(0.65 if di=='High' else 0.40 if di=='Medium' else 0.18)
            so=cr.random()<(0.72 if di=='High' else 0.55 if di=='Medium' else 0.25)
            co=cr.random()<0.90
            dnc=cr.random()<0.035
            if dnc: eo=so=co=False
            pref=('EMAIL' if di=='High' and eo else 'SMS' if so else 'CALL')
            if dnc: pref='CALL'
            chans.append(dict(member_key=mk,
                              email_allowed='true' if eo else 'false',
                              sms_allowed='true' if so else 'false',
                              call_allowed='true' if co else 'false',
                              preferred_channel=pref,
                              do_not_contact_flag='true' if dnc else 'false',
                              channel_risk_notes=''))
            ext.append(dict(mk=mk,pk=p['plan_key'],age=age,g=g,di=di,se=se,la=la,
                            diab=diab,cvd=cvd,stars=p['star_rating_current']))
    print(f'  {len(mems):,} members'); return mems, chans, ext

PLAN_MOD={'P001':-0.065,'P002':-0.030,'P003':0.000,'P004':0.040,'P005':0.070}

def prop(ex, m_ps, days, stars):
    """
    Realistic HEDIS propensity: targets ~18% T1, ~38% T2, ~44% T3 nationally.
    Adjusts based on member attributes (digital literacy, language, SES, age, days open).
    Uses a mixture-model approach: assign tier probabilistically, then sample within range.
    """
    r=rng(ex['mk']+str(m_ps)+'p')
    di=ex['di']; se=ex['se']; la=ex['la']; age=ex['age']

    # Base tier probabilities (national HEDIS benchmark)
    t1=0.18; t3=0.44

    # Attribute adjustments
    if di=='High':   t1+=0.12; t3-=0.10
    elif di=='Low':  t1-=0.08; t3+=0.12
    if la!='English': t1-=0.06; t3+=0.08
    if se=='High':   t1+=0.06; t3-=0.05
    elif se=='Low':  t1-=0.05; t3+=0.07
    if age<70:       t1+=0.04
    elif age>80:     t1-=0.06; t3+=0.06

    # Measure difficulty adjustment (prop_shift: positive=easier measure)
    t1 += m_ps * 0.3
    t3 -= m_ps * 0.3

    # Days open: longer-open = harder (push toward T3)
    if days>200: t1-=0.04; t3+=0.06
    elif days<60: t1+=0.03

    # Star rating: higher-star plans have harder remaining gaps
    if stars>=4.0: t1-=0.03; t3+=0.04

    # Clamp
    t1=max(0.04,min(0.50,t1)); t3=max(0.15,min(0.70,t3)); t2=max(0.05,1-t1-t3)

    tier=r.choices([1,2,3],weights=[t1,t2,t3])[0]
    if   tier==1: sc=r.gauss(0.800,0.065); sc=max(0.70,min(0.95,sc))
    elif tier==2: sc=r.gauss(0.570,0.070); sc=max(0.45,min(0.699,sc))
    else:         sc=r.gauss(0.275,0.100); sc=max(0.05,min(0.449,sc))
    return round(sc,4)

def gen_gaps(ext):
    today=date(2026,8,24); gaps=[]
    by_plan={}
    for e in ext: by_plan.setdefault(e['pk'],[]).append(e)

    for m in MEASURES:
        for p in PLANS:
            pk=p['plan_key']
            mod=PLAN_MOD[pk]
            comp=min(0.96,max(0.30, m['avg']+mod+rng(pk+m['mk']+'cr').gauss(0,0.012)))
            gap_r=1.0-comp
            for e in by_plan.get(pk,[]):
                age=e['age']
                if m['eg'] and e['g']!=m['eg']: continue
                if age<m['amin']: continue
                if m['amax'] and age>m['amax']: continue
                if m['diab'] and not e['diab']: continue
                if m['cvd']  and not e['cvd']:  continue

                gr=rng(e['mk']+m['mk']+'gs')
                if gr.random()>gap_r: continue

                do=(int(gr.gauss(72,28)) if gr.random()<0.54 else int(gr.gauss(258,58)))
                do=max(7,min(365,do))
                od=today-timedelta(days=do)
                gs=gr.choices(['Open','Borderline','Partial'],weights=[0.78,0.13,0.09])[0]
                pv=prop(e,m['ps'],do,e['stars'])

                if pv>=0.70:   pri='HIGH';   rc='SMS';  ri='FIT_KIT_MAILER'
                elif pv>=0.45: pri='MEDIUM'; rc='SMS';  ri='GIFTCARD_15'
                else:          pri='LOW';    rc='CALL'; ri='GIFTCARD_25'

                cr_=rng(e['mk']+m['mk']+'clin')
                clin=round(max(5,min(99,(1-pv)*80+cr_.gauss(15,10))),1)
                pyg=gr.random()<0.35

                gaps.append(dict(
                    member_gap_key=f"MGK_{e['mk']}_{m['mk']}",
                    member_key=e['mk'], measure_key=m['mk'], measure_code=m['mc'],
                    plan_key=pk, measurement_year=2025,
                    gap_status=gs, gap_open_date=str(od), gap_close_date=None,
                    days_open=do, clinical_risk_score=clin, nba_propensity_score=pv,
                    previous_year_gap_flag=1 if pyg else 0,
                    upstream_recommended_channel=rc, upstream_recommended_incentive=ri,
                    upstream_recommended_priority=pri,
                    last_outreach_date=None, last_outreach_channel=None,
                    is_suppressed='false', source_id='demo',
                ))
    print(f'  {len(gaps):,} gaps'); return gaps

def load(mems, chans, gaps):
    conn=sqlite3.connect(DB); cur=conn.cursor()

    cur.execute("DELETE FROM dim_member WHERE source_id='demo'")
    cur.executemany("""INSERT OR REPLACE INTO dim_member
        (member_key,dob_year,age_band,gender,language_preference,digital_literacy_segment,
         socioeconomic_segment,pcp_provider_key,display_name,source_id)
        VALUES(:member_key,:dob_year,:age_band,:gender,:language_preference,:digital_literacy_segment,
               :socioeconomic_segment,:pcp_provider_key,:display_name,:source_id)""", mems)

    cur.execute("""DELETE FROM dim_member_channel_pref WHERE member_key IN
        (SELECT member_key FROM dim_member WHERE source_id='demo')""")
    cur.executemany("""INSERT OR REPLACE INTO dim_member_channel_pref
        (member_key,email_allowed,sms_allowed,call_allowed,preferred_channel,do_not_contact_flag,channel_risk_notes)
        VALUES(:member_key,:email_allowed,:sms_allowed,:call_allowed,:preferred_channel,:do_not_contact_flag,:channel_risk_notes)
    """, chans)

    cur.execute("DELETE FROM fact_member_gap WHERE source_id='demo'")
    B=5000
    for i in range(0,len(gaps),B):
        cur.executemany("""INSERT OR REPLACE INTO fact_member_gap
            (member_gap_key,member_key,measure_key,measure_code,plan_key,measurement_year,
             gap_status,gap_open_date,gap_close_date,days_open,clinical_risk_score,nba_propensity_score,
             previous_year_gap_flag,upstream_recommended_channel,upstream_recommended_incentive,
             upstream_recommended_priority,last_outreach_date,last_outreach_channel,is_suppressed,source_id)
            VALUES(:member_gap_key,:member_key,:measure_key,:measure_code,:plan_key,:measurement_year,
                   :gap_status,:gap_open_date,:gap_close_date,:days_open,:clinical_risk_score,:nba_propensity_score,
                   :previous_year_gap_flag,:upstream_recommended_channel,:upstream_recommended_incentive,
                   :upstream_recommended_priority,:last_outreach_date,:last_outreach_channel,:is_suppressed,:source_id)
        """, gaps[i:i+B])
        print(f'  gaps {min(i+B,len(gaps)):,}/{len(gaps):,}…', end='\r')
    print()

    cur.execute("DELETE FROM dim_plan_contract WHERE source_id='demo'")
    cur.executemany("""INSERT OR REPLACE INTO dim_plan_contract
        (plan_key,contract_id,plan_name,region,segment,star_rating_current,star_rating_target,
         plan_annual_revenue,total_members,plan_pmpm_monthly,source_id)
        VALUES(:plan_key,:contract_id,:plan_name,:region,:segment,:star_rating_current,:star_rating_target,
               :plan_annual_revenue,:total_members,:plan_pmpm_monthly,:source_id)
    """, [dict(plan_key=p['plan_key'],contract_id=p['contract_id'],plan_name=p['plan_name'],
               region=p['region'],segment=p['segment'],star_rating_current=p['star_rating_current'],
               star_rating_target=p['star_rating_target'],plan_annual_revenue=p['n']*p['pmpm']*12,
               total_members=p['n'],plan_pmpm_monthly=p['pmpm'],source_id='demo') for p in PLANS])

    # dim_measure — check which columns exist
    cur.execute("PRAGMA table_info(dim_measure)")
    dm_cols={r[1] for r in cur.fetchall()}
    for m in MEASURES:
        row={k:v for k,v in dict(
            measure_key=m['mk'],measure_code=m['mc'],measure_name=m['name'],
            measure_type='HEDIS',star_weight=m['sw'],hedis_domain=m['cat'],
            age_gender_eligibility=f"age {m['amin']}–{m['amax'] or 'max'}"+(' diab' if m['diab'] else '')+(' cvd' if m['cvd'] else ''),
            clinical_description=m['name'],nba_default_playbook='STANDARD_OUTREACH',
        ).items() if k in dm_cols}
        cur.execute(f"INSERT OR REPLACE INTO dim_measure ({','.join(row.keys())}) VALUES ({','.join(':'+k for k in row.keys())})", row)

    conn.commit(); conn.close(); print('SQLite done.')

def export_csv(mems, chans, gaps):
    os.makedirs('input',exist_ok=True)
    def w(p,rows):
        with open(p,'w',newline='',encoding='utf-8') as f:
            wr=csv.DictWriter(f,fieldnames=rows[0].keys()); wr.writeheader(); wr.writerows(rows)
        print(f'  {p}: {len(rows):,} rows')
    w('input/dim_member.csv',mems)
    w('input/dim_member_channel_pref.csv',chans)
    w('input/fact_member_gap.csv',gaps)
    w('input/dim_plan_contract.csv',[dict(plan_key=p['plan_key'],contract_id=p['contract_id'],
        plan_name=p['plan_name'],region=p['region'],segment=p['segment'],
        star_rating_current=p['star_rating_current'],star_rating_target=p['star_rating_target'],
        plan_annual_revenue=p['n']*p['pmpm']*12,total_members=p['n'],plan_pmpm_monthly=p['pmpm'],
        source_id='demo') for p in PLANS])

def summary(mems,gaps):
    from collections import Counter
    print('\n'+'='*60)
    by_plan=Counter(g['plan_key'] for g in gaps)
    for p in PLANS:
        print(f"  {p['plan_name'][:40]} ({p['star_rating_current']}★)  members={p['n']:,}  gaps={by_plan[p['plan_key']]:,}")
    tiers=Counter('T1' if g['nba_propensity_score']>=0.70 else 'T2' if g['nba_propensity_score']>=0.45 else 'T3' for g in gaps)
    tot=len(gaps)
    print(f'\n  Total members: {len(mems):,}  |  Total gaps: {tot:,}')
    for k in ['T1','T2','T3']:
        n=tiers[k]; print(f'  {k}: {n:,} ({100*n//tot}%)')
    by_m=Counter(g['measure_code'] for g in gaps)
    print('\n  By measure:')
    for m in MEASURES:
        print(f"    {m['mc']:4}: {by_m[m['mc']]:,}  (NCQA avg {m['avg']:.1%}, top decile {m['td']:.1%})")
    print('='*60)

if __name__=='__main__':
    print('Generating members…')
    mems,chans,ext=gen_members()
    print('Generating gaps…')
    gaps=gen_gaps(ext)
    print('Loading to SQLite…')
    load(mems,chans,gaps)
    print('Exporting CSVs…')
    export_csv(mems,chans,gaps)
    summary(mems,gaps)
    print('\nDone.')
