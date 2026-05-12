# NBA_Claude_DemoV1 Replay Notes

## Purpose

These notes describe how to **reset the demo environment** between runs of NBA_Claude_DemoV1 so that a new live session starts from a clean, header-only state in `output/`. They are intended for demo operators, not end users, and complement `docs/demo-runbook.md` (which covers the live narrative).

## Current V1 default state

**Version 1 currently ships with a worked example already present in `output/`.** The four output CSVs contain the rows produced by an earlier successful end-to-end run (`RUN_PHASE1_20260512_104500`: EED in Plan P003, two cohorts, two campaigns, five planned contacts, five trace rows).

This is **intentional**. The worked example serves two purposes:

- It lets a new presenter inspect realistic output artifacts before going live.
- It demonstrates the shape and join-ability of the four output files (`fact_nba_claude_decision.csv`, `dim_nba_campaign.csv`, `fact_nba_outreach_plan.csv`, `fact_nba_trace.csv`) without having to run the demo first.

Operators can choose to walk through this example as part of the demo, or reset it before going live — both are supported.

## When to reset

Reset the outputs when any of the following applies:

- You are about to run a **live, recorded demo** and want a clean slate so the audience sees rows appear in real time.
- You want to **re-record screenshots** that show empty output files transitioning to populated ones.
- A previous run produced rows you no longer want mixed with the next run (note: this is rare — different runs are naturally separated by their `nba_run_id`).
- You are handing the repo to a new operator and want them to experience the demo cold.

If none of the above applies, leave `output/` as-is and rely on `nba_run_id` to keep runs distinguishable.

## Files to reset

Only the four output CSVs may be reset. Reset means **truncate the file to its header row only** — do not delete the file, do not change column order, do not modify column names.

- `output/fact_nba_claude_decision.csv`
- `output/dim_nba_campaign.csv`
- `output/fact_nba_outreach_plan.csv`
- `output/fact_nba_trace.csv`

**Never modify** any file under `input/`. The seed data in `input/` is part of the canonical demo state and is treated as read-only during a run.

## Safe reset method

1. Open each of the four `output/` CSVs.
2. Keep the first line (the header) exactly as it is.
3. Delete every line below the header.
4. Save the file with no trailing data rows (a final newline is fine).
5. Repeat for all four files.

Do not delete and recreate the files: the headers, column order, and quoting style must match the schema the agents expect. Editing in place is the safest method.

The exact headers to preserve are documented in `docs/demo-runbook.md` under "Reset guidance" and in `docs/data-dictionary.md`.

## Recommended operator prompt

Operators can either paste the natural-language prompt below, or invoke the project shortcut **`/nba-reset`** (defined at `.claude/commands/nba-reset.md`) — both produce the same header-only reset of the four output CSVs.

```
Reset NBA_Claude_DemoV1 output files for a fresh demo run. Keep the headers exactly as they are and remove all data rows from every CSV in output/.
```

Claude should then truncate each of the four output CSVs to its header row only, confirm row counts (each file should report 0 data rows after reset), and leave every file in `input/` untouched.

## Validation after reset

After resetting, confirm:

- [ ] All four `output/` CSVs exist (no file was accidentally deleted).
- [ ] Each output CSV contains exactly one line: the header.
- [ ] Header text and column order are byte-for-byte identical to the schema.
- [ ] No file under `input/` was modified (timestamps unchanged is a quick check).
- [ ] No new files were created in `output/` or anywhere else.

A simple visual check (open each file, confirm only the header is present) is sufficient before the next live demo.

## Notes

- Reset is a **stateless** operation — there is nothing else to clear (no caches, no derived files, no temporary state).
- `nba_run_id` is regenerated automatically at the start of each new session, so previous runs do not collide with new ones even without a reset. Reset is a presentation-cleanliness tool, not a correctness requirement.
- If a reset is performed in error, the worked-example rows can be re-created by running the four phases end to end against the seed data in `input/`, using the same opportunity (EED in Plan P003) and the same Option-1 campaign design.
