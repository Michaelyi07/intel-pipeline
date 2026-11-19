# Changelog

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
