# intel-pipeline — Intelligence Entry Processing Pipeline Orchestration & Failure Re-run

Step-wise orchestrator for intelligence entries: eleven ordered steps, per-step
records, model-step retry with backoff, resume-from-failed-step reprocessing, and
a Redis queue that pauses as a whole when the model gateway is unavailable.

Python 3.11+ / FastAPI / SQLAlchemy 2 / Alembic / PostgreSQL 16 / Redis. Fictional
test data only; no real personal or enterprise data; no client name in deliverables;
env-only config/secrets; no overseas SDKs.

## Pipeline (step diagram)

```
 new page captured
        |
        v
  +-----------+     +-----------+     +-----------+     +------------+
  |  extract  | --> | normalize | --> |   dedup   | --> | link_check |
  | (body txt)|     | (dates/orgs)    | (exact/near)    | (threshold)|
  +-----------+     +-----------+     +-----+-----+     +-----+------+
                                            |                 |
                                 repost -> END               | below threshold
                                 (discarded,                 v -> END
                                  0 model calls)        (discarded)
        +---------------------------------------------------+
        v
  +-----------+     +------------+     +------------+     +--------------+
  |  redact   | --> |    tag     | --> | summarize  | --> |  conditions  |
  | (model)   |     |  (model)   |     |  (model)   |     |   (model)    |
  +-----------+     +------------+     +------------+     +--------------+
        |  fail: retry x2 (backoff 1 min / 5 min) -> processing_failed
        v
  +--------------+     +-----------+     +------------+     +----------+
  | build_checks | --> |   match   | --> |  in_check  | ==> | human    |
  | (num/date    |     | (model)   |     | (queue     |     | verify   |
  |  alignment)  |     |           |     |  marker)   |     | queue    |
  +--------------+     +-----------+     +------------+     +----------+
```

Every step writes one row to `intel_item_steps`
(item_id, step, status, attempt, error_code, duration_ms) plus the unified
convention columns (org_id, site_id, created_at, created_by, updated_at, deleted_at).

Rules implemented:

1. A failed step stops the chain; remaining steps are recorded `skipped`.
2. A repost (dedup) or link-threshold drop ends the flow at that step (discarded).
3. Model steps (redact, tag, summarize, conditions, match) auto-retry twice with
   1 min / 5 min backoff; on final failure the item status is `processing_failed`.
4. `POST /api/intel/items/{id}/reprocess` re-runs an item starting from the first
   non-success step; completed steps are not re-executed.
5. When the model gateway is unavailable, the whole Redis queue pauses (no items
   pulled, no per-item failures); processing resumes after recovery.
6. Worker concurrency is controlled via `INTEL_WORKER_CONCURRENCY`.

## Repo layout

| File | Purpose |
|---|---|
| `app/orchestrator.py` | Pipeline orchestration loop (retry/backoff, short-circuit, resume) |
| `app/steps.py` | Step implementations + `ModelGateway` stub (integration point) |
| `app/models.py` | `intel_items` + `intel_item_steps` SQLAlchemy 2 models |
| `app/worker.py` | Redis queue + worker (gateway health gate, env concurrency) |
| `app/api.py` | `POST /api/intel/items/{id}/reprocess` (tenant-scoped) |
| `app/config.py` | All config from environment variables |
| `alembic.ini` + `migrations/alembic/` | Real Alembic revision `20261119_iip_001` |
| `migrations/001_intel_item_steps.sql` | Equivalent plain-SQL DDL for psql pipelines |
| `tests/` | Unit + integration tests (real PostgreSQL 16, fakeredis queue) |
| `docs/adr/` | Architecture decision records |
| `scripts/smoke_demo.py` | One-command fictional-entry demo printing the step ledger |

## Run & test

Requirements: Python 3.11+, PostgreSQL 16, Redis (tests use fakeredis, no server needed).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # Windows: .venv\Scripts\pip
cp .env.example .env                                                 # fill DATABASE_URL
export DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test

# apply the migration (Alembic; or psql -f migrations/001_intel_item_steps.sql)
alembic -x db_url="$DATABASE_URL" upgrade head

pytest tests -v          # 18 tests against real PostgreSQL — no SQLite, no DB-layer mocks
python scripts/smoke_demo.py   # end-to-end fictional entry demo
```

`make test` / `make demo` do the same via the Makefile.

Env vars (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://...5433/intel_pipeline_test` | PostgreSQL 16 URL |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis for the queue |
| `INTEL_WORKER_CONCURRENCY` | `2` | worker concurrency (env-controlled) |
| `MODEL_GATEWAY_URL` / `MODEL_GATEWAY_TIMEOUT_SECONDS` | stub endpoint / 10 | gateway client config |
| `MODEL_RETRY_MAX_ATTEMPTS` | `3` | 1 initial + 2 retries |
| `INTEL_LINK_CHECK_MIN_SCORE` | `60` | original-link threshold |

## Integration points (dependency stubs, per the brief)

- **Model gateway** — `app/steps.py::ModelGateway` is the stub. The real gateway
  ticket replaces `invoke()` (or sets `MODEL_GATEWAY_URL` against an HTTP client).
- **Crawler ticket** — sets `intel_items.link_score` (original-link threshold score)
  and `source_url` when enqueuing a captured page.
- **Dedup store** — `Orchestrator(known_fingerprints=...)` accepts the seen-fingerprint
  set; the dedup ticket swaps in a real store behind the same interface.
- **Workbench/IAM** — `app/api.py` reads tenant headers (`X-Org-Id`/`X-Site-Id`) from
  the trusted gateway; every lookup is tenant-scoped (cross-tenant item → 404).
- **Alembic chain** — revision `20261119_iip_001` upgrades/downgrades cleanly in the
  core team's migration chain.

## Verification record

Environment: Windows x64; Python 3.13.14; PostgreSQL **16.9** (Visual C++ build 1943,
64-bit) real instance at `127.0.0.1:5433`; SQLAlchemy 2.1.8; psycopg 3.3.9; FastAPI
0.165.0; pytest 9.2.0; fakeredis 2.40.0. Tests run against real PostgreSQL — no
SQLite, no DB-layer mocks. Raw output captured in `docs/verification.md` and in CI.

## Known limitations

- Step bodies are stubs by design (model/gateway implementations are out of scope);
  the orchestration rules are the deliverable and are fully tested.
- The reprocess endpoint trusts gateway-injected tenant headers (integration point
  documented above); real authn is the IAM ticket's scope.
