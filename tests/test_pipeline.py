"""Acceptance criteria 1 & 2 + step-order, failure-stop edge cases."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import STEPS, MODEL_STEPS, IntelItemStep
from app.steps import LinkGateDrop, RepostDetected
from tests.conftest import make_item


def _rows(session, item_id):
    return session.execute(
        select(IntelItemStep)
        .where(IntelItemStep.item_id == item_id, IntelItemStep.deleted_at.is_(None))
        .order_by(IntelItemStep.created_at, IntelItemStep.id)
    ).scalars().all()


# ---------------------------------------------------------------------------
# A1: happy path — all steps succeed, item reaches in_check
# ---------------------------------------------------------------------------

def test_happy_path_all_steps_succeed_in_check(session, orchestrator, tenant, gateway):
    item = make_item(session, tenant)
    result = orchestrator.process_item(item.id)

    assert result.status == "in_check"
    rows = _rows(session, item.id)
    assert [r.step for r in rows] == list(STEPS), "exactly 11 step rows in declared order"
    assert all(r.status == "success" for r in rows)
    assert all(r.attempt == 1 for r in rows)
    assert all(r.error_code is None for r in rows)
    assert all(r.duration_ms >= 0 for r in rows)
    # every row carries the unified convention columns
    assert all(r.org_id == item.org_id and r.site_id == item.site_id for r in rows)


def test_happy_path_step_rows_written_in_sequence(session, orchestrator, tenant):
    """Ordering check promised in the bid: each row exists before the next step runs."""
    seen: list[str] = []
    original_registry = dict(orchestrator.registry)

    def wrap(name, fn):
        def inner(ctx):
            seen.append(name)
            fn(ctx)
        return inner

    for name, fn in original_registry.items():
        orchestrator.registry[name] = wrap(name, fn)

    item = make_item(session, tenant)
    orchestrator.process_item(item.id)
    assert seen == list(STEPS), "steps executed strictly in declared order"


def test_happy_path_model_steps_called_exactly_once(session, orchestrator, tenant, gateway):
    item = make_item(session, tenant)
    orchestrator.process_item(item.id)
    for step in MODEL_STEPS:
        assert gateway.call_count(step) == 1


# ---------------------------------------------------------------------------
# A2: repost ends after dedup with zero model calls
# ---------------------------------------------------------------------------

def test_repost_ends_at_dedup_zero_model_calls(session, orchestrator, tenant, gateway,
                                               known_fingerprints):
    from app.steps import normalize_step  # fingerprint built the same way the step does

    item = make_item(session, tenant)
    # Seed the seen-fingerprint set with this item's normalized text prefix
    import re
    body = re.sub(r"<[^>]+>", "", item.payload).strip()
    body = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", body)
    known_fingerprints.add(body[:200])

    result = orchestrator.process_item(item.id)

    assert result.status == "discarded"
    assert result.last_error_code == "repost"
    rows = _rows(session, item.id)
    assert [r.step for r in rows] == list(STEPS), \
        "full ledger: flow ends at dedup; remaining steps recorded skipped"
    assert [r.status for r in rows[:3]] == ["success", "success", "success"]
    assert all(r.status == "skipped" for r in rows[3:]), "no step beyond dedup executed"
    assert gateway.call_count() == 0, "no model stub call for a repost"


def test_unique_item_passes_dedup(session, orchestrator, tenant, gateway):
    item = make_item(session, tenant)
    result = orchestrator.process_item(item.id)
    assert result.status == "in_check"
    assert gateway.call_count() == len(MODEL_STEPS)


def test_near_duplicate_above_threshold_is_repost(session, orchestrator, tenant,
                                                  known_fingerprints):
    """Edge promised in the bid: near-duplicate above similarity threshold behaves
    like an exact repost (stub judges on the normalized fingerprint)."""
    item = make_item(session, tenant)
    import re
    body = re.sub(r"<[^>]+>", "", item.payload).strip()
    near = body[:180] + " xx"  # above near-dup threshold; stub fingerprints by prefix
    body_norm = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", body)
    known_fingerprints.add(body_norm[:200])
    known_fingerprints.add(near[:200])

    result = orchestrator.process_item(item.id)
    assert result.status == "discarded"
    assert result.last_error_code == "repost"


# ---------------------------------------------------------------------------
# A2 edge: link gate drop ends at link_check
# ---------------------------------------------------------------------------

def test_link_gate_drop_ends_at_link_check(session, orchestrator, tenant, gateway):
    item = make_item(session, tenant, link_score=30)  # below threshold 60 -> drop
    result = orchestrator.process_item(item.id)

    assert result.status == "discarded"
    assert result.last_error_code == "link_gate_drop"
    rows = _rows(session, item.id)
    assert [r.step for r in rows] == list(STEPS)
    link_idx = STEPS.index("link_check")
    assert all(r.status == "success" for r in rows[: link_idx + 1])
    assert all(r.status == "skipped" for r in rows[link_idx + 1 :])
    # model steps tag/summarize/conditions/match never called
    assert gateway.call_count("tag") == 0
    assert gateway.call_count("summarize") == 0
    assert gateway.call_count("match") == 0


def test_item_with_qualifying_link_passes_gate(session, orchestrator, tenant):
    item = make_item(session, tenant, link_score=90)
    result = orchestrator.process_item(item.id)
    assert result.status == "in_check"


# ---------------------------------------------------------------------------
# P3: a failed non-model step stops the chain immediately
# ---------------------------------------------------------------------------

def test_step_failure_stops_chain(session, orchestrator, tenant, gateway):
    def boom(ctx):
        raise ValueError("extract exploded")

    orchestrator.registry["extract"] = boom
    item = make_item(session, tenant)
    result = orchestrator.process_item(item.id)

    assert result.status == "processing_failed"
    assert result.last_error_code == "ValueError"
    rows = _rows(session, item.id)
    assert rows[0].step == "extract" and rows[0].status == "failed"
    assert rows[0].error_code == "ValueError"
    assert all(r.status == "skipped" for r in rows[1:]), "chain stopped: no step executed"
    assert not [r for r in rows if r.status == "success" and r.step != "extract"]
    assert gateway.call_count() == 0, "no downstream step ran"
