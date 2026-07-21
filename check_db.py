import sqlite3
conn = sqlite3.connect('careintel.db')
print("=== GAP STATUS BREAKDOWN ===")
rows = conn.execute('SELECT gap_status, COUNT(*) FROM fact_member_gap GROUP BY gap_status').fetchall()
for r in rows: 
    print(r)


print("\n=== CLOSED GAPS ===")
rows = conn.execute('SELECT member_gap_key, member_key, measure_code, plan_key, gap_status FROM fact_member_gap where gap_status = \"Closed\"').fetchall()
for r in rows: 
    print(r)


print("\n=== LATEST SESSION OUTREACH ===")
rows = conn.execute('SELECT nba_run_id, contact_id, member_gap_key, channel, incentive_offered, status FROM fact_nba_outreach_plan ORDER by created_timestamp DESC LIMIT 10').fetchall()
for r in rows: 
    print(r)

print("\n=== ALL SESSIONS TRACE ===")
rows = conn.execute('SELECT nba_run_id, agent, step, output_summary FROM fact_nba_trace ORDER BY timestamp DESC LIMIT 10').fetchall()
for r in rows: 
    print(r)

conn.close()