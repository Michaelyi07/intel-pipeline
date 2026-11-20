# Changelog

## [1.1.0] - 2026-11-20 (client review fixes)
- Gateway recovery is now automatic: the worker mirrors gateway health into the
  Redis pause flag in both directions (outage sets it, healthy gateway clears
  it); the resume test no longer calls `queue.resume()` manually.
- Gateway unavailability is a distinct exception (`GatewayUnavailableError`,
  `ConnectionError` subclass). When it hits an item mid-processing: no
  retry/backoff burn, the attempt is recorded `failed/gateway_unavailable`, the
  item returns to `pending` and is requeued (resumable, never counted as
  failed), and the queue pauses. Test added (outage begins at redact).
- Dedup is now exact + near-duplicate: character 5-gram shingle Jaccard
  similarity with threshold from `INTEL_DEDUP_SIMILARITY_THRESHOLD`
  (default 0.85, env-controlled, validated). The near-duplicate test seeds only
  a lightly-edited variant (no exact fingerprint); a dissimilar-text pass test
  added.
- Worker concurrency is real parallelism: a thread pool processes up to
  `INTEL_WORKER_CONCURRENCY` items concurrently (thread-safe per-task
  orchestrator/session factory). Concurrency proven by a 2-party barrier test.
- Step status literal unified to `succeeded` (was `success`) across model,
  migration comments, tests, README and ADRs.
- Alembic revision builds fresh Column objects per table (no shared instances
  across `op.create_table` calls); new test runs `upgrade head → business case
  → downgrade base` on an empty database.
- 23 tests (unit + integration + migration) against real PostgreSQL 16; all passing.

## [1.0.0] - 2026-11-19
- Initial delivery: eleven-step orchestrator (extract → normalize → dedup →
  link_check → redact → tag → summarize → conditions → build_checks → match →
  in_check) with per-step `intel_item_steps` ledger rows.
- Alembic revision `20261119_iip_001` (upgrade/downgrade) + plain-SQL DDL.
- Model-step retry: 2 retries, backoff 1 min / 5 min, then `processing_failed`.
- `POST /api/intel/items/{id}/reprocess` resumes from the first non-success
  step, tenant-scoped via trusted headers.
- Redis queue with env-controlled worker concurrency; queue-level pause when
  the model gateway is unavailable; automatic resume after recovery.
- 18 tests (unit + integration) against real PostgreSQL 16; all passing.
