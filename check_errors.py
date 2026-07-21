import sqlite3
conn = sqlite3.connect("careintel.db")
rows = conn.execute("""
    SELECT contact_id, channel, status, error_reason
    FROM fact_nba_outreach_plan
    WHERE nba_run_id = 'RUN_20260703_164002'
""").fetchall()
for r in rows:
    print(r[0], "|", r[1], "|", r[2])
    if r[3]:
        print("  ERROR:", r[3][:200])
conn.close()
