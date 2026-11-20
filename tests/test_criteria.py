"""Acceptance criteria 3 & 4: retry/backoff, processing_failed, reprocess resume,
gateway-unavailable queue pause, automatic recovery and mid-item outage pause."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select

from app.models import MODEL_STEPS, IntelItemStep
from app.steps import ModelGateway
from tests.conftest import make_item


def _rows(session, item_id):
    return session.execute(
        select(IntelItemStep)
        .where(IntelItemStep.item_id == item_id, IntelItemStep.deleted_at.is_(None))
        .order_by(IntelItemStep.created_at, IntelItemStep.id)
    ).scalars().all()


def _succeeded_steps(session, item_id) -> set[str]:
    return {r.step for r in _rows(session, item_id) if r.status == "succeeded"}


# ---------------------------------------------------------------------------
# A3: consecutive model invocation failures -> processing_failed; re-run from failed step
# (Model-invocation errors are ordinary failures: retried with backoff, then the
# item is marked processing_failed. Gateway *unavailability* is a distinct
# exception handled as a queue pause — see the A4 section below.)
# ---------------------------------------------------------------------------

def test_model_retry_backoff_then_processing_failed(session, tenant, gateway, registry):
    from app.orchestrator import Orchestrator

    def always_fail(step_name, data):
        raise RuntimeError("model invocation error")

    gateway.invoke = always_fail  # invocation-level failure, not gateway outage
    sleeps: list[float] = []
    orch = Orchestrator(session, registry, sleep=sleeps.append)

    item = make_item(session, tenant)
    result = orch.process_item(item.id)

    assert result.status == "processing_failed"
    # First model step is redact: 1 initial + 2 retries = attempt 3 recorded
    rows = _rows(session, item.id)
    redact_rows = [r for r in rows if r.step == "redact"]
    assert [r.attempt for r in redact_rows] == [1, 2, 3]
    assert all(r.status == "failed" for r in redact_rows)
    assert all(r.error_code == "RuntimeError" for r in redact_rows)
    # backoff 1 min / 5 min (ticket requirement)
    assert sleeps == [60, 300]
    # steps after redact were not executed
    assert "tag" not in _succeeded_steps(session, item.id)
    assert result.last_error_code == "RuntimeError"


def test_transient_success_on_second_retry_proceeds(session, tenant, gateway, registry):
    """Edge promised in the bid: success on retry two proceeds without failing."""
    from app.orchestrator import Orchestrator

    calls = {"redact": 0}
    original_invoke = gateway.invoke

    def flaky(step_name, data):
        if step_name == "redact":
            calls["redact"] += 1
            if calls["redact"] == 1:
                raise RuntimeError("transient invocation error")
        return original_invoke(step_name, data)

    gateway.invoke = flaky
    sleeps: list[float] = []
    orch = Orchestrator(session, registry, sleep=sleeps.append)

    item = make_item(session, tenant)
    result = orch.process_item(item.id)

    assert result.status == "in_check"
    assert calls["redact"] == 2
    assert sleeps == [60]  # one backoff before the successful retry
    rows = [r for r in _rows(session, item.id) if r.step == "redact"]
    assert [(r.status, r.attempt) for r in rows] == [("failed", 1), ("succeeded", 2)]


def test_reprocess_resumes_from_failed_step(session, tenant, gateway, registry):
    """A3 core: re-run starts from the failed step; earlier steps are NOT re-executed."""
    from app.orchestrator import Orchestrator

    original_invoke = gateway.invoke

    def always_fail(step_name, data):
        raise RuntimeError("model invocation error")

    gateway.invoke = always_fail
    orch = Orchestrator(session, registry, sleep=lambda s: None)
    item = make_item(session, tenant)
    orch.process_item(item.id)
    assert item.status == "processing_failed"

    earlier_succeeded_before = _succeeded_steps(session, item.id)
    assert earlier_succeeded_before == {"extract", "normalize", "dedup", "link_check"}

    # Invocation errors stop; gateway recovers (invoke restored); reprocess
    gateway.invoke = original_invoke
    result = orch.process_item(item.id)

    assert result.status == "in_check"
    # extract/normalize/dedup/link_check were not re-run (no new rows for them)
    rows = [r for r in _rows(session, item.id) if r.step in earlier_succeeded_before]
    assert len(rows) == 4, "completed steps keep exactly one succeeded row each"
    assert all(r.status == "succeeded" and r.attempt == 1 for r in rows)
    assert _succeeded_steps(session, item.id) == set(MODEL_STEPS) | {
        "extract", "normalize", "dedup", "link_check", "build_checks", "in_check",
    }
    # the failed redact attempts keep their history; the new run adds a succeeded row
    redact_rows = [r for r in _rows(session, item.id) if r.step == "redact"]
    assert [r.status for r in redact_rows] == ["failed", "failed", "failed", "succeeded"]


def test_reprocess_api_endpoint_scoped_and_resumes(session, tenant, gateway, registry):
    """HTTP-level check of POST /api/intel/items/{id}/reprocess (workbench integration)."""
    import app.api as api_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.orchestrator import Orchestrator

    original_invoke = gateway.invoke

    def always_fail(step_name, data):
        raise RuntimeError("model invocation error")

    gateway.invoke = always_fail
    orch = Orchestrator(session, registry, sleep=lambda s: None)
    api_module.ReprocessService.orchestrator = orch

    app = FastAPI()
    app.include_router(api_module.router)
    client = TestClient(app, raise_server_exceptions=False)

    item = make_item(session, tenant)
    orch.process_item(item.id)
    org_id, site_id = tenant

    # missing tenant headers -> 400
    resp = client.post(f"/api/intel/items/{item.id}/reprocess")
    assert resp.status_code == 400

    # cross-tenant headers -> 404 (indistinguishable from missing)
    resp = client.post(f"/api/intel/items/{item.id}/reprocess",
                       headers={"X-Org-Id": str(uuid.uuid4()), "X-Site-Id": site_id})
    assert resp.status_code == 404

    # owner tenant + invocation errors stop (invoke restored) -> resumes from failed step
    gateway.invoke = original_invoke
    resp = client.post(f"/api/intel/items/{item.id}/reprocess",
                       headers={"X-Org-Id": org_id, "X-Site-Id": site_id})
    assert resp.status_code == 200
    assert resp.json() == {"id": str(item.id), "status": "in_check"}

    # already in_check -> 409 (not reprocessable)
    resp = client.post(f"/api/intel/items/{item.id}/reprocess",
                       headers={"X-Org-Id": org_id, "X-Site-Id": site_id})
    assert resp.status_code == 409


def test_malformed_tenant_ids_rejected(session):
    import app.api as api_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.orchestrator import Orchestrator
    from app.steps import build_step_registry, ModelGateway

    gw = ModelGateway()
    orch = Orchestrator(session, build_step_registry(gw), sleep=lambda s: None)
    api_module.ReprocessService.orchestrator = orch
    app = FastAPI()
    app.include_router(api_module.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/intel/items/not-a-uuid/reprocess",
                       headers={"X-Org-Id": "x", "X-Site-Id": "y"})
    assert resp.status_code == 422  # invalid path UUID rejected by FastAPI validation


# ---------------------------------------------------------------------------
# A4: gateway unavailable -> queue pauses; processing resumes automatically
# ---------------------------------------------------------------------------

def test_gateway_unavailable_pauses_queue(session, worker, queue, tenant, gateway):
    items = [make_item(session, tenant) for _ in range(3)]
    for it in items:
        queue.enqueue(it.id)

    gateway.set_unavailable(True)
    summary = worker.run_once()

    assert summary == {"paused": True, "processed": 0, "failed": 0}
    assert queue.is_paused()
    assert queue.depth() == 3, "no item pulled, no item failed — queue-level pause"
    for it in items:
        assert it.status == "pending", "items are NOT marked processing_failed during outage"


def test_queue_resumes_after_recovery(session, worker, queue, tenant, gateway):
    """No manual flag clear: switching the gateway stub back to available is the
    only action — the worker detects recovery and auto-clears the pause flag."""
    items = [make_item(session, tenant) for _ in range(4)]
    for it in items:
        queue.enqueue(it.id)

    gateway.set_unavailable(True)
    worker.run_once()
    assert queue.is_paused()

    # recovery: gateway back up — processing resumes without any manual resume()
    gateway.set_unavailable(False)
    summary = worker.run_until_empty()

    assert summary["paused"] is False
    assert summary["processed"] == 4
    assert summary["failed"] == 0
    assert queue.depth() == 0
    session.expire_all()
    for it in items:
        assert it.status == "in_check"


def test_queue_pauses_mid_item_when_gateway_dies(session, worker, queue, tenant, gateway):
    """Gateway turns unavailable while an item is being processed (at redact):
    the item is requeued resumable — NOT processing_failed — and the queue pauses;
    after recovery it continues from the failed step without re-running earlier ones."""
    original_invoke = gateway.invoke

    def die_at_redact(step_name, data):
        if step_name == "redact":
            gateway.set_unavailable(True)  # outage begins mid-item
        return original_invoke(step_name, data)

    gateway.invoke = die_at_redact
    item = make_item(session, tenant)
    queue.enqueue(item.id)

    summary = worker.run_once()
    assert summary["paused"] is True
    assert summary["failed"] == 0, "mid-item outage must not count as a per-item failure"
    assert queue.is_paused()
    assert queue.depth() == 1, "affected item requeued in resumable state, not dropped"

    session.expire_all()
    assert item.status == "pending", "item stays resumable (pending), not processing_failed"
    rows = _rows(session, item.id)
    redact_rows = [r for r in rows if r.step == "redact"]
    assert len(redact_rows) == 1, "no retry/backoff burn for a gateway outage"
    assert redact_rows[0].status == "failed"
    assert redact_rows[0].error_code == "gateway_unavailable"
    assert _succeeded_steps(session, item.id) == {"extract", "normalize", "dedup", "link_check"}

    # recovery: gateway back up — item continues from redact, earlier steps untouched
    gateway.set_unavailable(False)
    gateway.invoke = original_invoke
    summary = worker.run_until_empty()
    assert summary["paused"] is False
    assert summary["processed"] == 1

    session.expire_all()
    assert item.status == "in_check"
    assert queue.depth() == 0
    rows = _rows(session, item.id)
    for step in ("extract", "normalize", "dedup", "link_check"):
        step_rows = [r for r in rows if r.step == step and r.status == "succeeded"]
        assert len(step_rows) == 1, f"{step} keeps exactly one succeeded row after resume"
    redact_rows = [r for r in rows if r.step == "redact"]
    assert [r.status for r in redact_rows] == ["failed", "succeeded"]


def test_resume_no_duplicate_step_rows(session, worker, queue, tenant, gateway):
    """Edge promised in the bid: no duplicate processing after resume."""
    item = make_item(session, tenant)
    queue.enqueue(item.id)

    gateway.set_unavailable(True)
    worker.run_once()
    gateway.set_unavailable(False)
    worker.run_until_empty()
    worker.run_until_empty()  # second pass over an empty queue: nothing to redo

    session.expire_all()
    rows = _rows(session, item.id)
    succeeded = [(r.step, r.attempt) for r in rows if r.status == "succeeded"]
    assert len(succeeded) == len(set(succeeded)), "no duplicate succeeded rows after resume"
    assert item.status == "in_check"


# ---------------------------------------------------------------------------
# Worker concurrency: real parallelism, not a batch size
# ---------------------------------------------------------------------------

def test_worker_processes_items_concurrently(session, worker, queue, tenant, registry):
    """Two items must be processed at the same time: extract waits on a 2-party
    barrier, so a sequential (batch-style) worker would time out and fail."""
    barrier = threading.Barrier(2, timeout=10)
    original_extract = registry["extract"]

    def extract(ctx):
        barrier.wait()  # BrokenBarrierError (-> processing_failed) if run sequentially
        original_extract(ctx)

    registry["extract"] = extract
    items = [make_item(session, tenant) for _ in range(2)]
    for it in items:
        queue.enqueue(it.id)

    summary = worker.run_once()

    assert summary == {"paused": False, "processed": 2, "failed": 0}, \
        "both items succeeded only if extract ran concurrently (barrier of 2)"
    session.expire_all()
    for it in items:
        assert it.status == "in_check"


def test_worker_concurrency_from_env(monkeypatch):
    import importlib
    import app.config as config

    monkeypatch.setenv("INTEL_WORKER_CONCURRENCY", "5")
    importlib.reload(config)
    assert config.WORKER_CONCURRENCY == 5
    monkeypatch.setenv("INTEL_WORKER_CONCURRENCY", "not-a-number")
    with pytest.raises(RuntimeError):
        importlib.reload(config)
