# ADR-001: Per-attempt ledger rows with skipped-marker completion records

## Status
Adopted

## Background
The ticket requires each step to write `intel_item_steps` rows and requires
"re-run starts from the failed step". Two designs compete:

- A: write a row only when a step actually executes (success/failed); resume =
  first step without a success row.
- B: additionally write `skipped` rows for steps that never executed when the
  flow ends early (failure/repost/link-gate drop).

## Decision
Option B: the ledger always contains one row per declared step for every item,
with `status` ∈ {success, failed, skipped}. Resume scans for the first step
without a `success` row.

## Alternatives considered
- Option A: smaller table, but a reader cannot distinguish "step never reached"
  from "step record lost" without extra inference; the workbench dashboard would
  need extra queries to render a complete per-item progress view.

## Consequences
- 11 rows per item regardless of outcome; simple full-progress rendering.
- Resume logic is a single scan (`first step without success`), including the
  case where a later retry skips already-succeeded steps.
- Verification queue consumers must filter `status='success'` when counting
  executed steps. Recorded here so downstream tickets don't assume dense rows.
- Failed attempts append new rows per attempt (attempt 1..3 for model steps);
  history is preserved, never overwritten — consistent with the platform's
  audit-oriented conventions.

## Source
Team internal decision; aligns with engineering standard 02.5 (state machine for
resumable progress).
