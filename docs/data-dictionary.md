# NBA_Claude_DemoV1 Data Dictionary

## Overview

NBA_Claude_DemoV1 is a CSV-driven Next Best Action demo for Medicare Star/HEDIS measures. This data dictionary documents the schema of every CSV in the project — five **input** files (read-only, upstream-like enterprise data) and four **output** files (written by the four conceptual agents during a run). Each output file is keyed by `nba_run_id`, which is generated once per session and reused across all four output files for join-ability and traceability.

Phase ownership for output writes:

- **Phase 1 — Opportunity Agent** appends rows to `fact_nba_claude_decision.csv`.
- **Phase 2 — Segmentation Agent** updates cohort columns on those same rows.
- **Phase 3 — Campaign Design Agent** writes `dim_nba_campaign.csv` and refines channel/incentive/explanation columns on the decision rows.
- **Phase 4 — Outreach Agent** writes `fact_nba_outreach_plan.csv` and `fact_nba_trace.csv`.

## Input files

All input files live under `input/` and are treated as **read-only** during a run.

### `dim_measure.csv`

HEDIS/Star measure definitions. One row per measure (BCS, COL, EED in the demo seed).

| Column | Meaning |
| --- | --- |
| `measure_key` | Surrogate key for the measure (e.g., `M001`). |
| `measure_code` | Short HEDIS-style code (`BCS`, `COL`, `EED`). |
| `measure_name` | Full business name of the measure. |
| `measure_type` | Measure classification (e.g., `Process`, `Outcome`). |
| `star_weight` | Relative Star-program weight of the measure for the contract year. |
| `hedis_domain` | HEDIS domain grouping (e.g., "Effectiveness of Care - Prevention and Screening"). |
| `age_gender_eligibility` | Plain-language eligibility window (age band, gender, clinical context). |
| `clinical_description` | One- or two-sentence description of what the measure assesses. |
| `nba_default_playbook` | Reference token for the default NBA playbook tied to this measure. |

### `dim_plan_contract.csv`

Plan-level attributes. One row per plan/contract.

| Column | Meaning |
| --- | --- |
| `plan_key` | Surrogate key for the plan (e.g., `P001`). |
| `contract_id` | CMS-style contract identifier (e.g., `H1234`). |
| `plan_name` | Marketing or operational plan name. |
| `region` | Geographic region (e.g., `Northeast`). |
| `segment` | Plan segment (`MAPD`, `DSNP`, `MA-only`). |
| `star_rating_current` | Current overall Star rating (1.0–5.0, half-point scale). |
| `star_rating_target` | Target overall Star rating for the contract year. |

### `dim_member.csv`

Synthetic member demographics and segments. One row per member.

| Column | Meaning |
| --- | --- |
| `member_key` | Surrogate key for the member (e.g., `MBR0001`). |
| `dob_year` | Year of birth. |
| `age_band` | Five-year age band (e.g., `65-69`). |
| `gender` | Single-letter gender code (`F`, `M`). |
| `language_preference` | ISO-639-1 language code (`EN`, `ES`, `ZH`). |
| `digital_literacy_segment` | Ordinal segment: `Low`, `Medium`, `High`. |
| `socioeconomic_segment` | Ordinal segment: `Low`, `Mid`, `High`. |
| `pcp_provider_key` | Surrogate key for the member's primary care provider. |

### `dim_member_channel_pref.csv`

Member-level channel permissions and consent. One row per member.

| Column | Meaning |
| --- | --- |
| `member_key` | Foreign key to `dim_member.member_key`. |
| `email_allowed` | Boolean — member has consented to email outreach. |
| `sms_allowed` | Boolean — member has consented to SMS outreach. |
| `call_allowed` | Boolean — member has consented to voice/call outreach. |
| `preferred_channel` | Member's stated preferred channel (`EMAIL`, `SMS`, `CALL`, or `NONE`). |
| `do_not_contact_flag` | Boolean — global DNC flag; overrides per-channel permissions. |
| `channel_risk_notes` | Free-text note describing channel-specific risks or context. |

### `fact_member_gap.csv`

Core fact table. One row per member × measure × measurement year.

| Column | Meaning |
| --- | --- |
| `member_gap_key` | Surrogate key for the member-gap row (e.g., `G00001`). |
| `member_key` | Foreign key to `dim_member.member_key`. |
| `measure_key` | Foreign key to `dim_measure.measure_key`. |
| `measure_code` | Denormalized measure code copied from `dim_measure`. |
| `plan_key` | Foreign key to `dim_plan_contract.plan_key`. |
| `measurement_year` | Measurement year (e.g., `2026`). |
| `gap_status` | `Open`, `Partial`, `Borderline`, or `Closed`. |
| `gap_open_date` | ISO date the gap became open. |
| `gap_close_date` | ISO date the gap closed (blank if still open). |
| `days_open` | Number of days the gap has been open (vs. close date if closed, vs. today otherwise). |
| `clinical_risk_score` | 0.0–1.0 clinical risk score for the member at this gap. |
| `nba_propensity_score` | 0.0–1.0 propensity that the gap can be closed with outreach. |
| `previous_year_gap_flag` | Boolean — member had the same gap in the prior measurement year. |
| `upstream_recommended_channel` | Upstream-system recommended channel (`EMAIL`, `SMS`, `CALL`, `NONE`). |
| `upstream_recommended_incentive` | Upstream-system recommended incentive token (e.g., `GIFTCARD_25`, `TRANSPORT_VOUCHER`, `NONE`). |
| `upstream_recommended_priority` | Upstream-system priority (`High`, `Medium`, `Low`). |
| `last_outreach_date` | ISO date of the most recent outreach attempt, if any. |
| `last_outreach_channel` | Channel of the most recent outreach attempt, if any. |
| `is_suppressed` | Boolean — true if the gap is suppressed (DNC, ineligible, etc.) and must be excluded from targeting. |

## Output files

All output files live under `output/` and are written by the agents during a run. Headers and column order are fixed; all rows in a single session share one `nba_run_id`.

### `fact_nba_claude_decision.csv`

**Owned across phases.** One row per `(nba_run_id, member_gap_key)` for every gap brought into scope by the Opportunity Agent. The Segmentation Agent fills the cohort columns; the Campaign Design Agent fills the action/channel/incentive/explanation columns. Captures the final per-gap NBA decision for this run.

### `dim_nba_campaign.csv`

**Owned by the Campaign Design Agent (Phase 3).** One row per campaign per run, describing channel strategy, frequency plan, incentive strategy, message template, and the cohorts the campaign targets. Joins back to decision rows via `nba_run_id` and (indirectly) via `target_cohort_ids` ↔ `cohort_id`.

### `fact_nba_outreach_plan.csv`

**Owned by the Outreach Agent (Phase 4).** One row per **planned contact attempt** generated by expanding campaigns across their targeted member-gaps. Includes channel, planned datetime, message template, incentive offered, and status (`PLANNED` at write time). Joins to decisions via `member_gap_key` and to campaigns via `campaign_id`.

### `fact_nba_trace.csv`

**Owned by the Outreach Agent (Phase 4), with milestone entries summarizing earlier phases.** One row per agent step — an audit trail of which agent did what, with short input/output summaries and `affected_population_count`. The recommended step set per run is `OPPORTUNITY_SELECTED`, `COHORTS_ASSIGNED`, `CAMPAIGNS_CREATED`, `OUTREACH_PLAN_CREATED`, and `RUN_SUMMARY`.
