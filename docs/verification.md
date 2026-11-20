# Verification record — intel-pipeline

## Environment (local run)

- OS: Windows x64 (bash via Git for Windows 2.55.0.windows.3)
- Python: 3.13.14
- PostgreSQL: **16.9** (Visual C++ build 1943, 64-bit), real instance at `127.0.0.1:5433`
- SQLAlchemy 2.1.8, psycopg 3.3.9, FastAPI 0.165.0, alembic 1.20.1, pytest 9.1.1, fakeredis 2.40.0
- Queue in tests: fakeredis 2.40.0 (no live Redis server needed); CI uses the same code path

## Migration check (clean database, real Alembic)

```
$ alembic -x db_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5433/migration_check" upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 20261119_iip_001, intel_item_steps + intel_items
$ alembic -x db_url="..." downgrade base
INFO  [alembic.runtime.migration] Running downgrade 20261119_iip_001 -> ,
$ psql -d migration_check -tc "select count(*) from pg_tables where tablename in ('intel_items','intel_item_steps');"
     0
$ alembic -x db_url="..." upgrade head   # re-upgrade OK
```

The upgrade→business-case→downgrade roundtrip is now ALSO asserted in-test:
`tests/test_migrations.py::test_migration_upgrade_business_case_downgrade` drops
the schema, runs `alembic upgrade head` programmatically, executes a full
fictional-entry pipeline on the migrated schema, then `downgrade base` and
asserts both tables are gone.

## Test run (real PostgreSQL 16.9 — no SQLite, no DB-layer mocks)

```
$ DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test python -m pytest tests -v
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.1.1, pluggy-1.6.0
rootdir: D:\WorkBuddyProjects\clawhunt\deliverables\2224_intel_pipeline
configfile: pytest.ini
collected 23 items

tests\test_criteria.py ...........                                       [ 47%]
tests\test_migrations.py .                                               [ 52%]
tests\test_pipeline.py ...........                                       [100%]

============================== warnings summary ===============================
tests/test_criteria.py::test_reprocess_api_endpoint_scoped_and_resumes
  .../fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated
tests/test_migrations.py::test_migration_upgrade_business_case_downgrade
  .../alembic/config.py:604: DeprecationWarning: No path_separator found in configuration

======================= 23 passed, 3 warnings in 4.90s ========================
EXIT=0
```

Full test list (23):

- test_criteria.py (11): test_model_retry_backoff_then_processing_failed,
  test_transient_success_on_second_retry_proceeds,
  test_reprocess_resumes_from_failed_step,
  test_reprocess_api_endpoint_scoped_and_resumes,
  test_malformed_tenant_ids_rejected, test_gateway_unavailable_pauses_queue,
  test_queue_resumes_after_recovery, test_queue_pauses_mid_item_when_gateway_dies,
  test_resume_no_duplicate_step_rows, test_worker_processes_items_concurrently,
  test_worker_concurrency_from_env
- test_migrations.py (1): test_migration_upgrade_business_case_downgrade
- test_pipeline.py (11): test_happy_path_all_steps_succeed_in_check,
  test_happy_path_step_rows_written_in_sequence,
  test_happy_path_model_steps_called_exactly_once,
  test_repost_ends_at_dedup_zero_model_calls, test_unique_item_passes_dedup,
  test_near_duplicate_above_threshold_is_repost,
  test_below_threshold_similar_text_passes_dedup, test_dedup_threshold_from_env,
  test_link_gate_drop_ends_at_link_check, test_item_with_qualifying_link_passes_gate,
  test_step_failure_stops_chain

Notes on the two review-driven behavior changes:

- `test_queue_resumes_after_recovery` no longer calls `queue.resume()`: switching
  the gateway stub back to available is the only action; the worker's pause-flag
  mirror clears the flag and processing resumes (auto-recovery).
- `test_queue_pauses_mid_item_when_gateway_dies` flips the gateway stub
  unavailable while the item is at `redact`: the item is requeued `pending`
  (resumable, error_code=gateway_unavailable, no retry burn, no skipped
  markers, queue paused), then continues from redact after recovery — the whole
  queue pauses instead of the item being marked processing_failed.

## Smoke demo (end-to-end fictional entry)

```
$ python scripts/smoke_demo.py
demo item 739c56f2-f0d8-4679-8c77-a68166c7d1d2 -> final status: in_check
  extract      succeeded attempt=1 err=None dur=0ms
  normalize    succeeded attempt=1 err=None dur=1ms
  dedup        succeeded attempt=1 err=None dur=0ms
  link_check   succeeded attempt=1 err=None dur=0ms
  redact       succeeded attempt=1 err=None dur=0ms
  tag          succeeded attempt=1 err=None dur=0ms
  summarize    succeeded attempt=1 err=None dur=0ms
  conditions   succeeded attempt=1 err=None dur=0ms
  build_checks succeeded attempt=1 err=None dur=0ms
  match        succeeded attempt=1 err=None dur=0ms
  in_check     succeeded attempt=1 err=None dur=0ms
```

## CI (real GitHub Actions run — postgres:16 service container)

- Repo: https://github.com/Michaelyi07/intel-pipeline (public)
- v1 run: https://github.com/Michaelyi07/intel-pipeline/actions/runs/44535418232
  (commit `8de51f6...` — conclusion **success**, 18 tests)
- v2 run: see the run linked from the commit that carries CHANGELOG 1.1.0
  (23 tests + migration + smoke demo); conclusion recorded in the delivery note.

CI steps: checkout → setup-python 3.12 → install deps → `alembic upgrade head`
against the postgres:16 service container → `pytest tests -v` → smoke demo.

## Acceptance criteria mapping

| # | Criterion | Test | Evidence |
|---|---|---|---|
| 1 | fictional entry, every step succeeded, ends in_check | `test_happy_path_all_steps_succeed_in_check` (+ sequence & call-count tests) | 11 rows status=succeeded, item.status=in_check; smoke demo |
| 2 | repost ends after dedup, zero model calls | `test_repost_ends_at_dedup_zero_model_calls`, `test_near_duplicate_above_threshold_is_repost`, `test_below_threshold_similar_text_passes_dedup` | gateway.call_count()==0; near-dup seed has NO exact fingerprint; dissimilar text passes |
| 3 | consecutive model failures → processing_failed; re-run from failed step | `test_model_retry_backoff_then_processing_failed`, `test_reprocess_resumes_from_failed_step`, API test | attempts [1,2,3], backoff [60,300]s asserted; earlier steps not re-executed |
| 4 | gateway unavailable → queue pauses; resumes after recovery (automatic; mid-item outage requeues resumable) | `test_gateway_unavailable_pauses_queue`, `test_queue_resumes_after_recovery` (no manual resume), `test_queue_pauses_mid_item_when_gateway_dies`, `test_resume_no_duplicate_step_rows` | items stay pending during outage; flag auto-cleared on recovery; mid-item outage keeps item pending + requeued |
