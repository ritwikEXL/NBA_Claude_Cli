# NBA_Claude_DemoV1 Demo Runbook

## Purpose

This runbook is the **live-demo script** for NBA_Claude_DemoV1. It is intended for internal presenters showing the CSV-driven Next Best Action (NBA) workflow to Medicare Advantage plan leaders, clinical operations stakeholders, and Star/HEDIS strategy teams. It captures the suggested narrative, the exact CLI phrases to type, and what to highlight at each beat so the demo runs in roughly 10–15 minutes without surprises.

> **Ships-with-example note.** NBA_Claude_DemoV1 currently ships with a worked example already present in `output/` (from `RUN_PHASE1_20260512_104500`). Operators can either review those artifacts as part of the walkthrough, or reset the outputs before a fresh live run by following `docs/replay-notes.md`.

## Demo objective

Show how a busy Medicare plan manager can move from "where is the biggest Star gap?" to "which members do I target, and how?" in a single guided conversation, with:

- A small set of business-framed opportunity choices.
- Interpretable, rule-based cohorts (no black-box segments).
- A campaign design that respects consent, language, and digital literacy.
- A concrete outreach plan and an auditable trace, all materialized as CSV artifacts.

Make clear that this demo deliberately uses **CSV files on the local filesystem** as a stand-in for an enterprise data layer. The same conversation will later be powered by Snowflake via the Snowflake MCP — the agent contracts do not change.

## Inputs used in the demo

All input files live under `input/` and are treated as **read-only** during a run.

- `dim_measure.csv` — HEDIS/Star measure definitions (BCS, COL, EED) with Star weight and eligibility.
- `dim_plan_contract.csv` — Plan-level context: contract ID, region, segment, current vs. target Star rating.
- `dim_member.csv` — Synthetic member demographics, language, digital literacy, socioeconomic segment.
- `dim_member_channel_pref.csv` — Channel permissions (email/SMS/call), preferred channel, DNC flags, risk notes.
- `fact_member_gap.csv` — Core gap fact: one row per member × measure × measurement year, with gap status, propensity, prior-year flag, and upstream recommendations.

## Outputs produced in the demo

All output files live under `output/` and are **written by the agents over the course of a run**.

- `fact_nba_claude_decision.csv` — One row per gap per run with cohort assignment, channel/incentive, priority score, expected lift, reason codes, and explanation.
- `dim_nba_campaign.csv` — One row per campaign per run (channel strategy, frequency plan, incentive strategy, message template).
- `fact_nba_outreach_plan.csv` — One row per planned contact attempt (channel, planned datetime, incentive, status).
- `fact_nba_trace.csv` — One row per agent step (audit trail of inputs, outputs, and affected counts).

All output rows for a given session share the same `nba_run_id`, which is the join key across the four files.

## Suggested live script

The demo is a single CLI conversation. The presenter plays the role of the plan manager. Below is the recommended turn-by-turn flow.

### Step 1 — Start the session

> Operator types either the full prompt or the project shortcut:
> - **`Start an NBA_Claude_DemoV1 Stars demo run.`**
> - or **`/nba`** (project-scoped slash command — equivalent shortcut).

- Claude announces a fresh `nba_run_id` (e.g., `RUN_YYYYMMDD_HHMMSS`) and orients the manager.
- Claude enters Phase 1 (Opportunity Selection) and presents 2–3 ranked opportunities derived from the input data.

### Step 2 — Choose the opportunity

> Operator types:
> **`Let's pursue the recommended opportunity.`**
> (or "A", or the specific measure × plan if you want to vary the demo)

- Claude previews exactly what will be written (chosen `measure × plan`, in-scope row count, `nba_run_id`).
- Claude appends rows to `fact_nba_claude_decision.csv` with `is_in_selected_opportunity = true` and an initial `priority_score`.

### Step 3 — Choose cohorts

- Claude proposes 2–4 rule-based cohorts within the chosen opportunity.

> Operator types:
> **`Let's target both cohorts.`**
> (or "Only C1" / "Only C2" if you want to demonstrate trade-offs)

- Claude updates `cohort_id`, `cohort_name`, and `cohort_priority_rank` on the in-scope rows in `fact_nba_claude_decision.csv`. No new rows are created.

### Step 4 — Choose the campaign option

- Claude presents one or two campaign options (channel mix, frequency, incentive, message theme) and recommends one.

> Operator types:
> **`Let's go with Option 1 as designed.`**

- Claude writes campaign rows to `dim_nba_campaign.csv` and fills `nba_action_type`, `final_channel`, `final_incentive`, `sla_days_to_contact`, `expected_gap_closure_lift`, `reason_codes`, `explanation_text`, and `decision_timestamp` on the in-scope decision rows.

### Step 5 — Review the outreach plan

- Claude summarizes the resulting outreach plan: planned contacts by channel and cohort, timeline, expected Star impact.

> Operator types:
> **`Proceed with the outreach plan.`**

- Claude writes per-contact rows to `fact_nba_outreach_plan.csv` (all `status = PLANNED`) and milestone rows to `fact_nba_trace.csv`, closing with a `RUN_SUMMARY` trace entry.

## Recommended demo storyline

Frame the conversation around a **DSNP plan with the largest Star headroom** (EED in Plan P003 in the seed data). The narrative arc:

1. **The problem.** "We have multiple Star gaps across plans and measures. Which one moves the needle the most this measurement year?"
2. **The opportunity.** Claude surfaces EED in the DSNP plan as the biggest Star upside, with an equity dimension that resonates with Medicare leaders.
3. **The cohorts.** Two distinct member profiles emerge naturally: a digitally engaged English-speaking member and a Spanish-speaking low-digital-literacy member with a prior-year gap. Same measure, very different barriers.
4. **The campaign.** Two lean cohort-specific campaigns — a low-cost digital nudge for the easy close, and a bilingual call plus transport voucher for the harder, higher-equity close.
5. **The plan.** A concrete 5-contact outreach plan over 14 days, fully auditable in CSV form, ready to scale to thousands of members in production.

End with: "Everything you just saw is a CSV trail. In production this is Snowflake plus the Snowflake MCP — same agent contracts, same conversation."

## What to highlight while presenting

- **Single guided conversation, four agent modes under the hood.** The plan manager never sees agent names or file paths.
- **Explainability.** Every recommendation comes with a one-sentence rationale and structured reason codes.
- **Consent-first design.** DNC and channel permissions are respected end to end; suppressed rows never enter the targeted cohorts.
- **Interpretable cohorts.** Rules are stated in plain English, not derived from opaque clustering.
- **Auditable run.** The `nba_run_id` joins every output row, and `fact_nba_trace.csv` reconstructs the decision history.
- **Portability.** The exact same workflow targets Snowflake later — only the I/O layer changes.

## End-of-demo checklist

Before closing the session, confirm:

- [ ] All four output CSVs now contain rows for the current `nba_run_id`.
- [ ] `fact_nba_claude_decision.csv` has cohort, channel, incentive, SLA, expected lift, reason codes, and explanation populated for every in-scope row.
- [ ] `dim_nba_campaign.csv` has one row per campaign in this run.
- [ ] `fact_nba_outreach_plan.csv` has one `PLANNED` row per planned contact.
- [ ] `fact_nba_trace.csv` has at least: `OPPORTUNITY_SELECTED`, `COHORTS_ASSIGNED`, `CAMPAIGNS_CREATED`, `OUTREACH_PLAN_CREATED`, `RUN_SUMMARY`.
- [ ] No file under `input/` was modified.
- [ ] The headers and column order in all four output files are unchanged.

## Extended Demo Options

These are **optional storyline variations** for audiences that want to see specific capabilities beyond the standard happy path. Each variation uses the same `/nba` entry point — only the opportunity and cohort choices differ.

### Variation 1 — CDC: The High-Stakes Chronic Condition Story

**Best for:** Clinical ops audiences, CMO/CMO-adjacent stakeholders, or when you want to emphasize Star weight impact.

**Setup context:** Controlling Blood Pressure (CDC) carries a `star_weight` of **2** — double the weight of BCS and the same as EED. P001 (Aurora MA-PD Choice) and P003 (Aurora DSNP Community) both have CDC gaps seeded in the data. This makes CDC a powerful "which measure moves the Star needle the most?" moment.

**Suggested narrative:**
1. Start `/nba` and note that when the Opportunity table appears, CDC entries will show `Star Weight: 2` alongside the gap count.
2. Point out to the audience: *"Same number of gaps, but closing a CDC gap earns twice the Star credit as closing a BCS gap — that's the business case for measure prioritization."*
3. Choose a CDC × P001 or CDC × P003 opportunity.
4. In cohort design, highlight that CDC members tend to have **higher clinical risk scores** (0.60–0.85 in the seed data) — the cohort rules naturally surface a "high-risk, lower-propensity" group that needs a different outreach strategy than a screening measure.
5. In campaign design, emphasize that no FIT kit or mammogram scheduling is involved — this is **pure behavior change outreach** (BP monitoring, medication adherence), which skews toward call-based channels and empathetic, low-literacy messaging.

**Key talking point:** *"CDC at star_weight 2 is where clinical outcomes and Star rating strategy converge. The NBA workflow doesn't just find the easiest closes — it helps you find the highest-value closes."*

---

### Variation 2 — COL: FIT_KIT_MAILER as the Primary Incentive

**Best for:** Operations and member experience audiences focused on access barriers and non-monetary incentives.

**Setup context:** COL (Colorectal Cancer Screening) has multiple eligible members across all four original plans. The campaign agent is now explicitly instructed to default to `FIT_KIT_MAILER` as the primary incentive for high-propensity COL cohorts — because mailing a FIT kit removes the scheduling burden entirely and is often more effective than a gift card for this measure.

**Suggested narrative:**
1. Start `/nba` and choose a COL opportunity (any plan has addressable COL gaps).
2. In campaign design, point out that Claude recommends `FIT_KIT_MAILER` for the C1 cohort rather than a gift card: *"For colorectal screening, the barrier is usually logistics, not motivation. A FIT kit in the mail removes that barrier — the member never has to schedule anything."*
3. For any cohort member flagged as low-SES, Claude will also surface `TRANSPORT_VOUCHER` as an alternative — demonstrate the two-incentive decision side by side.
4. Ask the audience: *"Which would you choose for a 68-year-old, English-speaking, digitally engaged member versus a 72-year-old Spanish-speaking, low-SES member?"* — the cohort rules answer this automatically.

**Key talking point:** *"This is what member-level personalization looks like at scale. The incentive isn't just 'a reward' — it's matched to the actual barrier the member faces."*

---

### Variation 3 — P005: The Turnaround Story

**Best for:** Executive audiences, board-level presentations, or when you want to show the NBA workflow applied to a plan with the most room to grow.

**Setup context:** Aurora MA-PD Signature (P005) has a current Star rating of **2.5** with a target of **3.0** — the largest absolute gap in the portfolio and the only plan below 3 stars. It has 10 seeded gaps across EED and BCS, with a mix of new members (MBR0011–MBR0020) representing the demographics the plan hasn't reached yet: older low-literacy members, Mandarin-speaking seniors, Spanish-speaking low-SES members, and a DNC member.

**Suggested narrative:**
1. Start `/nba` and note that P005 appears in the Opportunity table with `2.5 → 3.0 (+0.5★)`.
2. Point out that while the Star gap is "only" +0.5, the **starting point of 2.5 is existential** for a Medicare plan — below 3.0 triggers CMS sanctions and enrollment restrictions. *"This isn't an improvement story. This is a survival story."*
3. Choose an EED × P005 or BCS × P005 opportunity.
4. In cohort design, the new member pool surfaces natural complexity — a high-digital, high-propensity member sits alongside a Mandarin-speaking low-SES member and potentially a DNC member (who is correctly suppressed and excluded). Let the audience see the suppression logic working.
5. In campaign design, highlight the multi-language, multi-channel nature of the outreach: *"Three members, three languages, three different channel strategies — all from a single NBA session."*
6. Close with: *"P005 going from 2.5 to 3.0 isn't just a Star improvement. It's the difference between a plan that survives and one that faces CMS action. The NBA workflow finds the members who can close that gap — and it does it in one conversation."*

**Key talking point:** *"The stakes are highest for the plans at the bottom of the Star scale. This is where targeted, personalized outreach has the most leverage — and where the NBA workflow delivers the most value."*

---

### Variation tips for all three

- You can combine variations: e.g., open with the Opportunities tab on the dashboard to show the full ranked radar table, then switch to the CLI to run the chosen opportunity end to end.
- For P005 specifically, reset outputs first (`/nba-reset`) so the audience sees a clean slate with only P005 rows written — the audit trail is more compelling when it contains exactly one run.
- If an audience member asks "what about the DNC member?" — let Claude demonstrate the suppression: start a P005 run, note MBR0017 is excluded from cohorts, and show that no contact row is generated for them in `fact_nba_outreach_plan.csv`.

## Reset guidance

> **Quickest path:** invoke the project shortcut **`/nba-reset`** before a fresh live demo — it truncates each of the four `output/` CSVs back to header-only state in one step. See `docs/replay-notes.md` for details.

The demo is **idempotent on `nba_run_id`** — each session gets a fresh ID and appends new rows without rewriting earlier runs. For a clean repeat demo, choose one of these approaches:

- **Append-mode (recommended for back-to-back demos):** start a new session with a new `nba_run_id`. Previous run rows remain in `output/` as a visible audit trail and do not affect the new run.
- **Full reset (for first-time demos or screen recordings):** truncate each file in `output/` down to its header row only — do **not** delete the files and do **not** change column order. The four headers to preserve are:
  - `fact_nba_claude_decision.csv`: `nba_run_id,member_gap_key,member_key,measure_key,measure_code,plan_key,measurement_year,is_in_selected_opportunity,cohort_id,cohort_name,cohort_priority_rank,nba_action_type,final_channel,final_incentive,priority_score,sla_days_to_contact,expected_gap_closure_lift,reason_codes,explanation_text,decision_timestamp`
  - `dim_nba_campaign.csv`: `campaign_id,nba_run_id,measure_key,plan_key,campaign_name,target_cohort_ids,channel_strategy,frequency_plan,message_template_id,incentive_strategy,created_timestamp`
  - `fact_nba_outreach_plan.csv`: `nba_run_id,contact_id,member_gap_key,campaign_id,channel,planned_datetime,message_template_id,incentive_offered,status,created_timestamp`
  - `fact_nba_trace.csv`: `nba_run_id,timestamp,agent,step,input_summary,output_summary,affected_population_count`
- **Never** modify the files under `input/` between demos. The seed data is part of the canonical demo state.
