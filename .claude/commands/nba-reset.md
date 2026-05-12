---
description: Reset NBA_Claude_DemoV1 output CSVs to header-only state for a fresh demo run
---

You are running inside the **NBA_Claude_DemoV1** project. The operator just invoked `/nba-reset` to clear the demo output artifacts before a fresh live run.

## Start with a brief one-liner

Tell the operator, in **one short sentence**, that you are resetting the demo outputs in `output/` to header-only state for a fresh NBA_Claude_DemoV1 run. Do not narrate further before acting.

## Scope — what to touch

Reset **only** these four files, in place:

- `output/fact_nba_claude_decision.csv`
- `output/dim_nba_campaign.csv`
- `output/fact_nba_outreach_plan.csv`
- `output/fact_nba_trace.csv`

For each of the four files:

1. Read the existing file.
2. Keep the **first line (header) exactly as-is** — same text, same column order, same quoting style.
3. Remove every line below the header.
4. Write the file back so that it contains exactly one line: the header.

Do not delete and recreate the files. Edit in place.

## Strict no-touch list

Do **not** modify, create, or delete anything outside `output/`. In particular, you must not touch:

- Any file under `input/`.
- Any markdown file (including `CLAUDE.md`, files under `agents/`, and files under `docs/`).
- Anything under `.claude/commands/` (including this command file).
- Any other file at the repo root or in any other directory.

If you would need to write to any path outside the four listed `output/` CSVs to complete the reset, **stop and report the issue** instead of proceeding.

## Verify and report

After resetting, verify and report concisely:

- Each of the four `output/` CSVs still exists.
- Each contains **exactly one line** (the header) — report the line count per file.
- For each file, show the header line so the operator can visually confirm it is unchanged.
- Confirm explicitly that no file under `input/`, no markdown file, and no file under `.claude/commands/` was modified.

End with a single line telling the operator the demo is ready for a fresh `/nba` run.
