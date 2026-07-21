#!/usr/bin/env python3
"""
seed_expansion.py — Adds realistic gap volumes to fact_member_gap
and creates the plan_population table with true membership counts.

Safe to re-run: uses INSERT OR IGNORE throughout.
Run after seed_demo_data.py.
"""

import os
import sqlite3
import random
from datetime import date, timedelta

random.seed(42)

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "careintel.db"))
TODAY = date(2026, 7, 21)

print(f"[expand] DB path: {DB_PATH}")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")

# ── 1. plan_population table ──────────────────────────────────────────────────

conn.execute("""
CREATE TABLE IF NOT EXISTS plan_population (
    plan_key      TEXT PRIMARY KEY,
    total_members INTEGER,
    plan_revenue  INTEGER,
    last_updated  TEXT
)
""")

PLAN_POP = [
    ("P001", 45000, 450000000),
    ("P002", 38000, 380000000),
    ("P003", 28000, 280000000),
    ("P004", 52000, 520000000),
    ("P005", 18000, 180000000),
]

for pk, members, revenue in PLAN_POP:
    conn.execute(
        "INSERT OR REPLACE INTO plan_population (plan_key, total_members, plan_revenue, last_updated) VALUES (?,?,?,?)",
        (pk, members, revenue, str(TODAY))
    )

conn.commit()
print("[expand] plan_population seeded")

# ── 2. Target gap counts ──────────────────────────────────────────────────────

# Target open gap counts per plan x measure
TARGETS = {
    "P001": {"BCS": 800,  "COL": 1200, "EED": 350, "CDC": 900,  "MAD": 280,  "AFV": 1600, "SPC": 400},
    "P002": {"BCS": 650,  "COL": 950,  "EED": 280, "CDC": 720,  "MAD": 220,  "AFV": 1300, "SPC": 320},
    "P003": {"BCS": 520,  "COL": 780,  "EED": 230, "CDC": 620,  "MAD": 190,  "AFV": 980,  "SPC": 260},
    "P004": {"BCS": 900,  "COL": 1400, "EED": 420, "CDC": 1050, "MAD": 330,  "AFV": 1900, "SPC": 480},
    "P005": {"BCS": 320,  "COL": 480,  "EED": 140, "CDC": 360,  "MAD": 110,  "AFV": 650,  "SPC": 170},
}

# Load measure_key lookup
measure_map = {}
for row in conn.execute("SELECT measure_key, measure_code FROM dim_measure").fetchall():
    measure_map[row["measure_code"]] = row["measure_key"]

# Load member keys per plan from fact_member_gap (plan_key lives there)
member_keys_by_plan = {}
for row in conn.execute(
    "SELECT DISTINCT member_key, plan_key FROM fact_member_gap"
).fetchall():
    member_keys_by_plan.setdefault(row["plan_key"], []).append(row["member_key"])

# Fallback: if a plan has no existing gaps, use all members
all_members = [r[0] for r in conn.execute("SELECT member_key FROM dim_member").fetchall()]
for pk, _, _ in PLAN_POP:
    if pk not in member_keys_by_plan:
        member_keys_by_plan[pk] = all_members


def pick_channel(measure_code):
    if measure_code in ("MAD", "AFV"):
        return random.choices(["EMAIL", "SMS", "CALL"], weights=[60, 30, 10])[0]
    elif measure_code in ("BCS", "COL"):
        return random.choices(["EMAIL", "SMS", "CALL"], weights=[40, 35, 25])[0]
    else:  # EED, CDC, SPC
        return random.choices(["EMAIL", "SMS", "CALL"], weights=[30, 30, 40])[0]


def pick_incentive(measure_code):
    if measure_code == "COL":
        return random.choices(
            ["GIFTCARD_15", "GIFTCARD_25", "TRANSPORT_VOUCHER", "FIT_KIT_MAILER"],
            weights=[40, 35, 15, 10]
        )[0]
    else:
        return random.choices(
            ["GIFTCARD_15", "GIFTCARD_25", "TRANSPORT_VOUCHER", "GIFTCARD_15"],
            weights=[40, 35, 15, 10]
        )[0]


def pick_status():
    return random.choices(["Open", "Borderline", "Partial"], weights=[85, 10, 5])[0]


# ── 3. Count existing open gaps and insert the difference ────────────────────

total_inserted = 0

for plan_key, measure_targets in TARGETS.items():
    members = member_keys_by_plan.get(plan_key, [])
    if not members:
        print(f"[expand] WARNING: no members for {plan_key}, skipping")
        continue

    for measure_code, target_count in measure_targets.items():
        mk = measure_map.get(measure_code)
        if not mk:
            print(f"[expand] WARNING: no measure_key for {measure_code}, skipping")
            continue

        # Count existing open gaps
        existing_count = conn.execute(
            """SELECT COUNT(*) FROM fact_member_gap
               WHERE plan_key = ? AND measure_key = ?
                 AND LOWER(gap_status) IN ('open','borderline','partial')""",
            (plan_key, mk)
        ).fetchone()[0]

        need = target_count - existing_count
        if need <= 0:
            print(f"[expand] {plan_key}x{measure_code}: already has {existing_count} open gaps (target {target_count}), skipping")
            continue

        # Find start index for new keys
        max_key_row = conn.execute(
            """SELECT member_gap_key FROM fact_member_gap
               WHERE plan_key = ? AND measure_key = ?
                 AND member_gap_key LIKE 'G\\_' || ? || '\\_' || ? || '\\_%' ESCAPE '\\'
               ORDER BY member_gap_key DESC LIMIT 1""",
            (plan_key, mk, plan_key, measure_code)
        ).fetchone()

        start_idx = 1
        if max_key_row:
            key_str = max_key_row["member_gap_key"]
            parts = key_str.rsplit("_", 1)
            if len(parts) == 2:
                try:
                    start_idx = int(parts[-1]) + 1
                except ValueError:
                    start_idx = existing_count + 1

        print(f"[expand] {plan_key}x{measure_code}: existing={existing_count}, target={target_count}, inserting={need}")

        start_of_year = date(2026, 1, 1)
        end_seed = date(2026, 4, 1)
        date_range_days = (end_seed - start_of_year).days

        rows = []
        for i in range(need):
            gk = f"G_{plan_key}_{measure_code}_{(start_idx + i):05d}"
            member_key = members[(start_idx + i - 1) % len(members)]

            gap_open = start_of_year + timedelta(days=random.randint(0, date_range_days))
            days_open = (TODAY - gap_open).days

            status = pick_status()
            clinical_risk = round(random.uniform(0.35, 0.85), 2)
            propensity = round(random.uniform(0.30, 0.80), 2)
            prev_gap_flag = "true" if random.random() < 0.25 else "false"

            rows.append((
                gk, member_key, mk, measure_code, plan_key,
                status, 2026, days_open,
                propensity, clinical_risk,
                prev_gap_flag, "false",
            ))

        conn.executemany(
            """INSERT OR IGNORE INTO fact_member_gap
               (member_gap_key, member_key, measure_key, measure_code, plan_key,
                gap_status, measurement_year, days_open,
                nba_propensity_score, clinical_risk_score,
                previous_year_gap_flag, is_suppressed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows
        )
        conn.commit()
        total_inserted += need

print(f"\n[expand] Total new gap rows inserted: {total_inserted}")

# ── 4. Summary ────────────────────────────────────────────────────────────────

print()
print("=" * 55)
print("  seed_expansion.py — Complete")
print("=" * 55)

for pk, _, _ in PLAN_POP:
    open_count = conn.execute(
        """SELECT COUNT(*) FROM fact_member_gap
           WHERE plan_key = ? AND LOWER(gap_status) IN ('open','borderline','partial')""",
        (pk,)
    ).fetchone()[0]
    print(f"  {pk}: {open_count:,} open gaps")

total_gaps = conn.execute("SELECT COUNT(*) FROM fact_member_gap").fetchone()[0]
print(f"  Total fact_member_gap rows: {total_gaps:,}")
print("=" * 55)

conn.close()
