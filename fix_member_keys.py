"""
fix_member_keys.py
------------------
Generates dim_member + dim_member_channel_pref rows for every
fact_member_gap member_key (demo source) that has no matching
dim_member entry.  Uses a seeded RNG keyed to the member_key
so output is deterministic (re-running produces the same rows).
"""

import sqlite3, random, hashlib

DB_PATH = r'C:\Users\vmuser\Documents\NBA_Claude_Cli\careintel.db'

# ── demographic pools ──────────────────────────────────────────────────────────
AGE_BANDS  = ['65-69','70-74','75-79','80-84','85+']
AGE_BAND_W = [0.25, 0.28, 0.22, 0.15, 0.10]          # realistic Medicare dist

LANGUAGES  = ['English','English','English','English',  # ~75 % English
              'Spanish','Spanish',                       # ~12 %
              'Chinese','Korean','Vietnamese','Tagalog'] # remainder

DIGITAL    = ['High','High','Medium','Medium','Medium','Low','Low']
SOCIO      = ['Middle','Middle','Middle','Low','Low','High']
GENDERS    = ['M','M','F','F','F']                      # slight female skew

DOB_BY_BAND = {
    '65-69': (1955, 1960), '70-74': (1950, 1955),
    '75-79': (1945, 1950), '80-84': (1940, 1945), '85+': (1930, 1940),
}

FIRST_NAMES = [
    'James','Mary','Robert','Patricia','John','Linda','Michael','Barbara',
    'William','Dorothy','David','Susan','Richard','Jessica','Thomas','Sarah',
    'Charles','Karen','Christopher','Nancy','Daniel','Margaret','Matthew','Lisa',
    'Anthony','Betty','Mark','Dorothy','Donald','Sandra','Steven','Ashley',
    'Paul','Kimberly','Andrew','Emily','Kenneth','Donna','Joshua','Michelle',
]
LAST_NAMES = [
    'Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis',
    'Rodriguez','Martinez','Hernandez','Lopez','Gonzalez','Wilson','Anderson',
    'Thomas','Taylor','Moore','Jackson','Martin','Lee','Perez','Thompson',
    'White','Harris','Sanchez','Clark','Ramirez','Lewis','Robinson','Walker',
]

# channel probabilities (roughly realistic for Medicare seniors)
# format: (email_allowed, sms_allowed, call_allowed, preferred)
CHANNEL_PROFILES = [
    ('true',  'true',  'true',  'EMAIL', 0.30),
    ('true',  'true',  'true',  'SMS',   0.15),
    ('false', 'true',  'true',  'SMS',   0.15),
    ('false', 'false', 'true',  'CALL',  0.25),
    ('true',  'false', 'true',  'EMAIL', 0.10),
    ('false', 'false', 'false', 'CALL',  0.05),  # DNC
]

def seed_rng(member_key: str) -> random.Random:
    h = int(hashlib.md5(member_key.encode()).hexdigest(), 16)
    rng = random.Random(h)
    return rng

def gen_member_row(member_key: str):
    rng = seed_rng(member_key)
    age_band = rng.choices(AGE_BANDS, weights=AGE_BAND_W)[0]
    dob_lo, dob_hi = DOB_BY_BAND[age_band]
    dob_year = rng.randint(dob_lo, dob_hi - 1)
    gender = rng.choice(GENDERS)
    language = rng.choice(LANGUAGES)
    digital = rng.choice(DIGITAL)
    socio = rng.choice(SOCIO)

    # non-English seniors tend to have lower digital literacy
    if language != 'English' and digital == 'High':
        digital = 'Medium'

    fn = rng.choice(FIRST_NAMES)
    ln = rng.choice(LAST_NAMES)
    display = f'{fn} {ln}'
    pcp_key = f'PCP{rng.randint(1,50):03d}'

    return (member_key, dob_year, age_band, gender, language,
            digital, socio, pcp_key, display, 'demo')

def gen_channel_row(member_key: str):
    rng = seed_rng(member_key + '_chan')
    weights = [p[4] for p in CHANNEL_PROFILES]
    profile = rng.choices(CHANNEL_PROFILES, weights=weights)[0]
    email, sms, call_, pref, _ = profile
    dnc = 'true' if (email == 'false' and sms == 'false' and call_ == 'false') else 'false'
    note = 'DNC on all channels' if dnc == 'true' else ''
    return (member_key, email, sms, call_, pref, dnc, note)


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # Find gap member_keys with no matching dim_member row
    cur.execute("""
        SELECT DISTINCT g.member_key
        FROM   fact_member_gap g
        LEFT JOIN dim_member m ON m.member_key = g.member_key
        WHERE  g.source_id = 'demo'
          AND  m.member_key IS NULL
        ORDER  BY g.member_key
    """)
    missing = [r[0] for r in cur.fetchall()]
    print(f'Unmatched member_keys to backfill: {len(missing)}')
    if not missing:
        print('Nothing to do.')
        conn.close()
        return

    # Generate rows
    member_rows  = [gen_member_row(k)  for k in missing]
    channel_rows = [gen_channel_row(k) for k in missing]

    # Insert dim_member
    cur.executemany("""
        INSERT OR IGNORE INTO dim_member
            (member_key, dob_year, age_band, gender, language_preference,
             digital_literacy_segment, socioeconomic_segment,
             pcp_provider_key, display_name, source_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, member_rows)

    # Insert dim_member_channel_pref (table must exist)
    cur.execute("PRAGMA table_info(dim_member_channel_pref)")
    cols = [r[1] for r in cur.fetchall()]
    print('dim_member_channel_pref cols:', cols)

    cur.executemany("""
        INSERT OR IGNORE INTO dim_member_channel_pref
            (member_key, email_allowed, sms_allowed, call_allowed,
             preferred_channel, do_not_contact_flag, channel_risk_notes)
        VALUES (?,?,?,?,?,?,?)
    """, channel_rows)

    conn.commit()

    # Verify
    cur.execute("""
        SELECT COUNT(*) FROM fact_member_gap g
        JOIN dim_member m ON m.member_key = g.member_key
        WHERE g.source_id = 'demo'
    """)
    matched = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fact_member_gap WHERE source_id='demo'")
    total = cur.fetchone()[0]
    print(f'After fix: {matched}/{total} demo gaps have a matching dim_member ({100*matched//total}%)')

    # Spot-check a few
    cur.execute("""
        SELECT g.member_key, m.language_preference, m.digital_literacy_segment,
               cp.preferred_channel, cp.email_allowed
        FROM   fact_member_gap g
        JOIN   dim_member m          ON m.member_key  = g.member_key
        LEFT JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
        WHERE  g.source_id = 'demo'
        GROUP  BY g.member_key
        LIMIT 5
    """)
    print('Sample joined rows:')
    for r in cur.fetchall():
        print(' ', r)

    conn.close()
    print('Done.')


if __name__ == '__main__':
    main()
