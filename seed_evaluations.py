#!/usr/bin/env python3
"""Create evaluation tables and seed historical evaluation data."""
import sqlite3, os
from datetime import date, datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")

# ── Create tables ─────────────────────────────────────────────────────────────

conn.executescript("""
CREATE TABLE IF NOT EXISTS campaign_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    campaign_id TEXT,
    evaluation_date TEXT,
    evaluation_window INTEGER,
    total_members_contacted INTEGER,
    gaps_closed_actual INTEGER,
    gaps_closed_expected INTEGER,
    actual_closure_rate REAL,
    expected_closure_rate REAL,
    performance_status TEXT,
    stars_impact_actual REAL,
    stars_impact_projected REAL,
    executive_summary TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS member_evaluations (
    member_eval_id TEXT PRIMARY KEY,
    evaluation_id TEXT,
    nba_run_id TEXT,
    contact_id TEXT,
    member_gap_key TEXT,
    member_key TEXT,
    outreach_sent_date TEXT,
    gap_status_at_evaluation TEXT,
    days_since_outreach INTEGER,
    responded INTEGER DEFAULT 0,
    recommended_action TEXT,
    action_reason TEXT,
    follow_up_scheduled TEXT,
    created_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_schedule (
    schedule_id TEXT PRIMARY KEY,
    nba_run_id TEXT,
    campaign_id TEXT,
    scheduled_date TEXT,
    evaluation_window INTEGER,
    status TEXT DEFAULT 'PENDING',
    created_timestamp TEXT
);
""")
conn.commit()
print("Tables created.")

# ── Seed historical evaluations ───────────────────────────────────────────────

today = date.today()
now_iso = datetime.now().isoformat(timespec="seconds")

# Get all runs that have SENT outreach contacts
runs = conn.execute("""
    SELECT DISTINCT o.nba_run_id, c.campaign_id,
           MIN(o.sent_at) AS first_sent,
           COUNT(o.contact_id) AS total_contacts,
           SUM(CASE WHEN o.status IN ('SENT','SCHEDULED','COMPLETED') THEN 1 ELSE 0 END) AS sent_count
    FROM fact_nba_outreach_plan o
    LEFT JOIN dim_nba_campaign c ON c.nba_run_id = o.nba_run_id
    GROUP BY o.nba_run_id
    HAVING sent_count > 0
""").fetchall()

print(f"Found {len(runs)} runs with sent contacts.")

ACTIONS = ["ESCALATE_INCENTIVE","SWITCH_CHANNEL","CARE_MANAGER_CALL",
           "EXTEND_CAMPAIGN","NO_ACTION","NO_ACTION","NO_ACTION","CLOSE_CAMPAIGN"]

for run in runs:
    run_id     = run["nba_run_id"]
    camp_id    = run["campaign_id"] or f"CMP_{run_id[4:]}_01"
    total      = run["total_contacts"] or 1
    first_sent = run["first_sent"] or str(today - timedelta(days=14))
    try:
        sent_date = date.fromisoformat(first_sent[:10])
    except Exception:
        sent_date = today - timedelta(days=14)

    days_elapsed = (today - sent_date).days
    window = 7 if days_elapsed < 14 else (14 if days_elapsed < 30 else 30)
    exp_rate = 0.20 if window == 7 else (0.50 if window == 14 else 0.80)

    # Simulate realistic closure counts
    rng_seed = sum(ord(c) for c in run_id)
    closed_frac = 0.30 + (rng_seed % 50) / 100.0   # 30–80 %
    closed_actual = max(1, round(total * closed_frac))
    actual_rate = round(closed_actual / total, 3)
    exp_closed = max(1, round(total * exp_rate))

    diff = actual_rate - exp_rate
    if diff >= -0.10:
        status = "Overperforming" if diff > 0.10 else "On Track"
    else:
        status = "Underperforming"

    stars_proj   = round(0.05 * total, 2)
    stars_actual = round(stars_proj * actual_rate / max(exp_rate, 0.01), 2)

    summary = (
        f"Campaign {camp_id} evaluated at day {window}. "
        f"{closed_actual} of {total} members contacted have closed their gap "
        f"({round(actual_rate*100)}% actual vs {round(exp_rate*100)}% expected). "
        f"Status: {status}. "
        f"Estimated Stars impact: {stars_actual} points (projected {stars_proj}). "
        + ("Campaign is performing in line with expectations. Continue current strategy."
           if status == "On Track" else
           "Consider escalating incentives or switching channels for non-responders."
           if status == "Underperforming" else
           "Exceeding targets. Consider closing campaign early to reallocate resources.")
    )

    eval_id = f"EVAL_{run_id[4:]}_{window}D"
    eval_date = str(today)

    conn.execute("INSERT OR IGNORE INTO campaign_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        eval_id, run_id, camp_id, eval_date, window,
        total, closed_actual, exp_closed,
        actual_rate, exp_rate, status,
        stars_actual, stars_proj, summary, now_iso
    ))

    # Member evaluations
    contacts = conn.execute("""
        SELECT o.contact_id, o.member_gap_key, g.member_key, o.sent_at, o.channel,
               g.gap_status, g.clinical_risk_score,
               m.digital_literacy_segment, m.socioeconomic_segment,
               p.email_allowed, p.sms_allowed, p.call_allowed
        FROM fact_nba_outreach_plan o
        LEFT JOIN fact_member_gap g ON g.member_gap_key = o.member_gap_key
        LEFT JOIN dim_member m ON m.member_key = g.member_key
        LEFT JOIN dim_member_channel_pref p ON p.member_key = g.member_key
        WHERE o.nba_run_id = ?
          AND o.status IN ('SENT','SCHEDULED','COMPLETED')
    """, (run_id,)).fetchall()

    for idx, c in enumerate(contacts):
        responded = 1 if c["gap_status"] == "Closed" else 0
        days_since = (today - sent_date).days

        if responded:
            action = "NO_ACTION"
            reason = "Gap already closed following outreach"
        else:
            risk = c["clinical_risk_score"] or 0.5
            lit  = c["digital_literacy_segment"] or "Medium"
            soc  = c["socioeconomic_segment"] or "Mid"
            if risk >= 0.75:
                action = "CARE_MANAGER_CALL"
                reason = f"High clinical risk ({risk}) — escalate to care manager"
            elif days_since >= 21 and c["call_allowed"] == "true":
                action = "SWITCH_CHANNEL"
                reason = f"No response after {days_since} days — try alternative channel"
            elif soc == "Low":
                action = "ESCALATE_INCENTIVE"
                reason = "Low SES member — increase incentive to improve engagement"
            elif lit == "Low":
                action = "CARE_MANAGER_CALL"
                reason = "Low digital literacy — human outreach more effective"
            elif days_since <= 7:
                action = "EXTEND_CAMPAIGN"
                reason = "Within initial 7-day window — allow more time before escalation"
            else:
                action = "NO_ACTION"
                reason = "Member profile suggests no additional action warranted"

        mem_eval_id = f"MEVAL_{run_id[4:]}_{c['contact_id']}"
        follow_up = str(today + timedelta(days=7)) if action not in ("NO_ACTION","CLOSE_CAMPAIGN") else ""

        conn.execute("INSERT OR IGNORE INTO member_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mem_eval_id, eval_id, run_id, c["contact_id"],
            c["member_gap_key"], c["member_key"],
            c["sent_at"] or str(sent_date),
            c["gap_status"] or "Open",
            days_since, responded, action, reason,
            follow_up, now_iso
        ))

    # Schedule upcoming evaluations
    for w in [7, 14, 30]:
        sched_date = sent_date + timedelta(days=w)
        sched_id = f"SCHED_{run_id[4:]}_{w}D"
        sched_status = "COMPLETED" if sched_date <= today else "PENDING"
        conn.execute("INSERT OR IGNORE INTO evaluation_schedule VALUES (?,?,?,?,?,?,?)", (
            sched_id, run_id, camp_id, str(sched_date), w, sched_status, now_iso
        ))

    print(f"  {run_id}: {status} | {closed_actual}/{total} closed | window={window}d")

conn.commit()

# Verify
print("\n=== VERIFICATION ===")
print(f"campaign_evaluations: {conn.execute('SELECT COUNT(*) FROM campaign_evaluations').fetchone()[0]}")
print(f"member_evaluations:   {conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0]}")
print(f"evaluation_schedule:  {conn.execute('SELECT COUNT(*) FROM evaluation_schedule').fetchone()[0]}")
due = conn.execute("SELECT COUNT(*) FROM evaluation_schedule WHERE scheduled_date <= ? AND status='PENDING'",
                   (str(today),)).fetchone()[0]
print(f"Due now:              {due}")
conn.close()
print("Done.")
