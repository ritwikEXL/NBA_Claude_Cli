import os
for line in open('.env').read().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('=')
        os.environ[k.strip()] = v.strip()

import db_adapter
with db_adapter.get_db() as conn:
    m = conn.execute('SELECT COUNT(*) AS n FROM dim_member').fetchone()
    g = conn.execute('SELECT COUNT(*) AS n FROM fact_member_gap').fetchone()
    p = conn.execute('SELECT plan_key, total_members FROM plan_population').fetchall()
    print(f"Members:  {m['n']:,}")
    print(f"Gaps:     {g['n']:,}")
    print("Plans:")
    for r in p:
        print(f"  {r['plan_key']}: {r['total_members']:,} members")
    print()
    print("Snowflake is LIVE!")
