"""
normalize_channel_data.py
--------------------------
Normalizes dim_member_channel_pref and dim_member so the JS frontend
(which checks for 'true'/'false' strings and uppercase channel names)
gets consistent values.

Changes:
  dim_member_channel_pref:
    - email_allowed / sms_allowed / call_allowed / do_not_contact_flag:
        '1' → 'true'   '0' → 'false'   (already 'true'/'false' → unchanged)
    - preferred_channel:
        'Email' → 'EMAIL'
        'Phone' → 'CALL'
        'Mail'  → 'EMAIL'    (treat mail-preference as email-adjacent)
        'SMS'   → 'SMS'      (unchanged)
        'CALL'  → 'CALL'     (unchanged)
        'EMAIL' → 'EMAIL'    (unchanged)

  dim_member:
    - language_preference:
        'EN' → 'English'
        'ES' → 'Spanish'
        'ZH' → 'Chinese'
        'KO' → 'Korean'
        'VI' → 'Vietnamese'
        'TL' → 'Tagalog'
    - digital_literacy_segment: already OK (High/Medium/Low)
    - socioeconomic_segment: already OK (High/Mid/Low)

Also: if preferred_channel is EMAIL but email_allowed is 'false',
  fall back to SMS if sms_allowed='true', else CALL.
"""

import sqlite3

DB = r'C:\Users\vmuser\Documents\NBA_Claude_Cli\careintel.db'

CHANNEL_MAP = {
    'Email': 'EMAIL', 'email': 'EMAIL',
    'Phone': 'CALL',  'phone': 'CALL',
    'Mail':  'EMAIL', 'mail':  'EMAIL',
    'Call':  'CALL',  'call':  'CALL',
    'Sms':   'SMS',   'sms':   'SMS',
    # already correct
    'EMAIL': 'EMAIL', 'SMS': 'SMS', 'CALL': 'CALL',
}

LANG_MAP = {
    'EN': 'English', 'ES': 'Spanish', 'ZH': 'Chinese',
    'KO': 'Korean',  'VI': 'Vietnamese', 'TL': 'Tagalog',
    'CN': 'Chinese', 'MN': 'Mandarin',
}

def to_bool(v):
    if v in ('1', 'true', 'True', 'TRUE', 1): return 'true'
    return 'false'

def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    # ── 1. Normalize channel_pref booleans + preferred_channel ──────────────
    cur.execute("SELECT rowid, * FROM dim_member_channel_pref")
    rows = cur.fetchall()
    updates = []
    for r in rows:
        email = to_bool(r['email_allowed'])
        sms   = to_bool(r['sms_allowed'])
        call_ = to_bool(r['call_allowed'])
        dnc   = to_bool(r['do_not_contact_flag'])

        raw_pref = r['preferred_channel'] or 'CALL'
        pref = CHANNEL_MAP.get(raw_pref, 'CALL')

        # Consistency: if preferred channel is blocked, pick next available
        if pref == 'EMAIL' and email == 'false':
            pref = 'SMS' if sms == 'true' else 'CALL'
        elif pref == 'SMS' and sms == 'false':
            pref = 'EMAIL' if email == 'true' else 'CALL'
        elif pref == 'CALL' and call_ == 'false':
            pref = 'SMS' if sms == 'true' else 'EMAIL' if email == 'true' else 'CALL'

        # DNC: if all blocked, mark DNC
        if email == 'false' and sms == 'false' and call_ == 'false':
            dnc = 'true'

        updates.append((email, sms, call_, pref, dnc, r['rowid']))

    cur.executemany("""
        UPDATE dim_member_channel_pref
        SET email_allowed=?, sms_allowed=?, call_allowed=?,
            preferred_channel=?, do_not_contact_flag=?
        WHERE rowid=?
    """, updates)
    print(f'Normalized {len(updates)} channel_pref rows')

    # ── 2. Normalize language codes ──────────────────────────────────────────
    cur.execute("SELECT rowid, language_preference FROM dim_member WHERE language_preference IS NOT NULL")
    lang_rows = cur.fetchall()
    lang_updates = []
    for r in lang_rows:
        lp = r['language_preference']
        mapped = LANG_MAP.get(lp, lp)  # keep as-is if already English/Spanish/etc
        if mapped != lp:
            lang_updates.append((mapped, r['rowid']))

    if lang_updates:
        cur.executemany("UPDATE dim_member SET language_preference=? WHERE rowid=?", lang_updates)
        print(f'Normalized {len(lang_updates)} language_preference values')
    else:
        print('Language preferences already normalized')

    conn.commit()

    # ── 3. Verification ──────────────────────────────────────────────────────
    print("\n=== After normalization ===")

    cur.execute("SELECT preferred_channel, COUNT(*) FROM dim_member_channel_pref GROUP BY preferred_channel ORDER BY 2 DESC")
    print("preferred_channel:", dict(cur.fetchall()))

    cur.execute("SELECT email_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY email_allowed")
    print("email_allowed:", dict(cur.fetchall()))

    cur.execute("SELECT sms_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY sms_allowed")
    print("sms_allowed:", dict(cur.fetchall()))

    cur.execute("SELECT call_allowed, COUNT(*) FROM dim_member_channel_pref GROUP BY call_allowed")
    print("call_allowed:", dict(cur.fetchall()))

    cur.execute("SELECT language_preference, COUNT(*) FROM dim_member WHERE source_id='demo' GROUP BY language_preference ORDER BY 2 DESC")
    print("language (demo):", dict(cur.fetchall()))

    # Channel breakdown for COL gaps
    print("\n=== COL gaps: channel availability ===")
    cur.execute("""
        SELECT cp.preferred_channel,
               cp.email_allowed,
               cp.sms_allowed,
               cp.call_allowed,
               COUNT(*) cnt
        FROM fact_member_gap g
        JOIN dim_member_channel_pref cp ON cp.member_key = g.member_key
        WHERE g.source_id='demo' AND g.measure_code='COL'
          AND LOWER(g.gap_status) IN ('open','borderline','partial')
        GROUP BY 1,2,3,4
        ORDER BY cnt DESC
        LIMIT 10
    """)
    for r in cur.fetchall():
        print(f"  pref={r[0]:5} email={r[1]:5} sms={r[2]:5} call={r[3]:5} n={r[4]}")

    conn.close()
    print("\nDone.")

if __name__ == '__main__':
    main()
