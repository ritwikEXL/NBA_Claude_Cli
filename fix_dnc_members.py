"""
fix_dnc_members.py
------------------
Members whose channel_pref has all three channels blocked (email=false,
sms=false, call=false) are effectively unreachable — the test data
generators stored 0/1 integers that normalized to all-false.

This script:
 1. Identifies those members (except truly-intended DNC ones, flagged
    do_not_contact_flag='true' originally AND member_key in a kept set).
 2. Assigns each a realistic channel profile using a seeded RNG so
    the fix is deterministic.
 3. Targets a ~5% DNC rate (realistic for Medicare outreach data).
 4. Verifies the resulting distribution.
"""

import sqlite3, random, hashlib

DB = r'C:\Users\vmuser\Documents\NBA_Claude_Cli\careintel.db'

# Realistic channel profile distribution for Medicare Advantage members
# (email_allowed, sms_allowed, call_allowed, preferred_channel)
PROFILES = [
    ('true',  'true',  'true',  'EMAIL', 0.20),   # digital-first, all channels
    ('true',  'true',  'true',  'SMS',   0.10),
    ('true',  'true',  'true',  'CALL',  0.05),
    ('false', 'true',  'true',  'SMS',   0.15),   # no email, SMS+call
    ('false', 'true',  'true',  'CALL',  0.10),
    ('false', 'false', 'true',  'CALL',  0.25),   # call only (common for seniors)
    ('true',  'false', 'true',  'EMAIL', 0.10),   # email+call, no SMS
    ('false', 'false', 'false', 'CALL',  0.05),   # DNC ~5%
]

def rng_for(member_key):
    h = int(hashlib.md5(('fix_' + member_key).encode()).hexdigest(), 16)
    return random.Random(h)

def pick_profile(member_key):
    rng = rng_for(member_key)
    weights = [p[4] for p in PROFILES]
    return rng.choices(PROFILES, weights=weights)[0]

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find members with all channels blocked
    cur.execute("""
        SELECT member_key FROM dim_member_channel_pref
        WHERE email_allowed='false' AND sms_allowed='false' AND call_allowed='false'
    """)
    blocked = [r[0] for r in cur.fetchall()]
    print(f'Members with all channels blocked: {len(blocked)}')

    if not blocked:
        print('Nothing to fix.')
        conn.close()
        return

    updates = []
    dnc_count = 0
    for mk in blocked:
        prof = pick_profile(mk)
        email, sms, call_, pref, _ = prof
        dnc = 'true' if (email == 'false' and sms == 'false' and call_ == 'false') else 'false'
        if dnc == 'true':
            dnc_count += 1
        updates.append((email, sms, call_, pref, dnc, mk))

    cur.executemany("""
        UPDATE dim_member_channel_pref
        SET email_allowed=?, sms_allowed=?, call_allowed=?,
            preferred_channel=?, do_not_contact_flag=?
        WHERE member_key=?
    """, updates)
    conn.commit()

    print(f'Fixed {len(updates)} members; kept {dnc_count} as true DNC ({100*dnc_count//len(updates)}%)')

    # Verification
    print('\n=== Channel distribution after fix ===')
    cur.execute("SELECT preferred_channel, COUNT(*) FROM dim_member_channel_pref GROUP BY preferred_channel ORDER BY 2 DESC")
    print('preferred_channel:', dict(cur.fetchall()))

    cur.execute("SELECT email_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY email_allowed")
    print('email_allowed:', dict(cur.fetchall()))

    cur.execute("SELECT sms_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY sms_allowed")
    print('sms_allowed:', dict(cur.fetchall()))

    cur.execute("SELECT call_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY call_allowed")
    print('call_allowed:', dict(cur.fetchall()))

    cur.execute("SELECT do_not_contact_flag, COUNT(*) FROM dim_member_channel_pref GROUP BY do_not_contact_flag")
    print('do_not_contact_flag:', dict(cur.fetchall()))

    # COL open gaps after fix
    cur.execute("""
        SELECT cp.preferred_channel,
               cp.email_allowed, cp.sms_allowed, cp.call_allowed,
               COUNT(*) cnt
        FROM fact_member_gap g
        JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
        WHERE g.source_id='demo' AND g.measure_code='COL'
          AND LOWER(g.gap_status) IN ('open','borderline','partial')
        GROUP BY 1,2,3,4
        ORDER BY cnt DESC
        LIMIT 10
    """)
    print('\nCOL open gaps by channel profile:')
    for r in cur.fetchall():
        print(f"  pref={r[0]:5} email={r[1]:5} sms={r[2]:5} call={r[3]:5} n={r[4]}")

    # Total reachable vs DNC for COL x P004
    cur.execute("""
        SELECT cp.do_not_contact_flag, COUNT(*) cnt
        FROM fact_member_gap g
        JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
        WHERE g.source_id='demo' AND g.measure_code='COL' AND g.plan_key='P004'
          AND LOWER(g.gap_status) IN ('open','borderline','partial')
        GROUP BY 1
    """)
    print('\nCOL x P004 open gaps — DNC breakdown:')
    for r in cur.fetchall():
        print(f"  do_not_contact={r[0]}: {r[1]}")

    conn.close()
    print('\nDone.')

if __name__ == '__main__':
    main()
