# Verification record — intel-pipeline

## Environment (local run)

- OS: Windows x64 (bash via Git for Windows 2.55.0.windows.3)
- Python: 3.13.14
- PostgreSQL: **16.9** (Visual C++ build 1943, 64-bit), real instance at `127.0.0.1:5433`
- SQLAlchemy 2.1.8, psycopg 3.3.9, FastAPI 0.165.0, alembic 1.20.1, pytest 9.2.0, fakeredis 2.40.0
- Queue in tests: fakeredis 2.40.0 (no live Redis server needed); CI uses the same code path

## Migration check (clean database, real Alembic)

```
$ alembic -x db_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5433/migration_check" upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 20261119_iip_001, intel_item_steps + intel_items
$ psql -d migration_check -c "\d intel_item_steps"   # table created with FK + indexes
$ alembic -x db_url="..." downgrade base
INFO  [alembic.runtime.migration] Running downgrade 20261119_iip_001 -> ,
$ psql -d migration_check -tc "select count(*) from pg_tables where tablename in ('intel_items','intel_item_steps');"
     0
$ alembic -x db_url="..." upgrade head   # re-upgrade OK
```

## Test run (real PostgreSQL 16.9 — no SQLite, no DB-layer mocks)

```
$ DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test python -m pytest tests -v
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.2.0, pluggy-1.7.0
rootdir: D:\intel-pipeline
configfile: pytest.ini
collected 18 items

tests\test_criteria.py .... nine tests:
  test_model_retry_backoff_then_processing_failed PASSED
  test_transient_success_on_second_retry_proceeds PASSED
  test_reprocess_resumes_from_failed_step PASSED
  test_reprocess_api_endpoint_scoped_and_resumes PASSED
  test_malformed_tenant_ids_rejected PASSED
  test_gateway_unavailable_pauses_queue PASSED
  test_queue_resumes_after_recovery PASSED
  test_resume_no_duplicate_step_rows PASSED
  test_worker_concurrency_from_env PASSED
tests\test_pipeline.py .... nine tests:
  test_happy_path_all_steps_succeed_in_check PASSED
  test_happy_path_step_rows_written_in_sequence PASSED
  test_happy_path_model_steps_called_exactly_once PASSED
  test_repost_ends_at_dedup_zero_model_calls PASSED
  test_unique_item_passes_dedup PASSED
  test_near_duplicate_above_threshold_is_repost PASSED
  test_link_gate_drop_ends_at_link_check PASSED
  test_item_with_qualifying_link_passes_gate PASSED
  test_step_failure_stops_chain PASSED

============================= 18 passed in 6.36s ==============================
EXIT=0
```

## Smoke demo (end-to-end fictional entry)

```
$ python scripts/smoke_demo.py
demo item 35a9d813-... -> final status: in_check
  extract      success  attempt=1 err=None dur=0ms
  normalize    success  attempt=1 err=None dur=1ms
  dedup        success  attempt=1 err=None dur=0ms
  link_check   success  attempt=1 err=None dur=0ms
  redact       success  attempt=1 err=None dur=0ms
  tag          success  attempt=1 err=None dur=0ms
  summarize    success  attempt=1 err=None dur=0ms
  conditions   success  attempt=1 err=None dur=0ms
  build_checks success  attempt=1 err=None dur=0ms
  match        success  attempt=1 err=None dur=0ms
  in_check     success  attempt=1 err=None dur=0ms
```

## CI (real GitHub Actions run — postgres:16 service container)

- Repo: https://github.com/Michaelyi07/intel-pipeline (public)
- Run: https://github.com/Michaelyi07/intel-pipeline/actions/runs/44535418232
- Commit: `8de51f6486ab4941d82bbf2e5a64576894286315` — conclusion **success**
- Raw CI log lines:

```
2026-11-19T13:38:34.9712937Z ======================== 18 passed, 1 warning in 0.76s =========================
2026-11-19T13:38:35.5213904Z demo item 7abb6b59-ecda-455b-8631-f7bb65eb9ff7 -> final status: in_check
```

CI steps: checkout → setup-python 3.12 → install deps → `alembic upgrade head`
against the postgres:16 service container → `pytest tests -v` → smoke demo.

## Acceptance criteria mapping

| # | Criterion | Test | Evidence |
|---|---|---|---|
| 1 | fictional entry, every step succeeds, ends in_check | `test_happy_path_all_steps_succeed_in_check` (+ sequence & call-count tests) | 11 rows status=success, item.status=in_check; smoke demo |
| 2 | repost ends after dedup, zero model calls | `test_repost_ends_at_dedup_zero_model_calls` (+ near-duplicate edge) | gateway.call_count()==0, remaining steps skipped |
| 3 | consecutive model failures → processing_failed; re-run from failed step | `test_model_retry_backoff_then_processing_failed`, `test_reprocess_resumes_from_failed_step`, API test | attempts [1,2,3], backoff [60,300]s asserted; earlier steps not re-executed |
| 4 | gateway unavailable → queue pauses; resumes after recovery | `test_gateway_unavailable_pauses_queue`, `test_queue_resumes_after_recovery`, `test_resume_no_duplicate_step_rows` | items stay pending during outage; all in_check after resume |
