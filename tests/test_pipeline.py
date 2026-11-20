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
    assert all(r.status == "succeeded" for r in rows)
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

def _normalized_body(payload: str) -> str:
    """Replicate extract+normalize the same way the steps do (for seeding fingerprints)."""
    import re
    body = re.sub(r"<[^>]+>", "", payload).strip()
    return re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", body)


def test_repost_ends_at_dedup_zero_model_calls(session, orchestrator, tenant, gateway,
                                               known_fingerprints):
    item = make_item(session, tenant)
    # Seed the seen set with the EXACT normalized text of this item (fingerprint
    # = full normalized text, same representation the dedup step compares).
    known_fingerprints.add(_normalized_body(item.payload))

    result = orchestrator.process_item(item.id)

    assert result.status == "discarded"
    assert result.last_error_code == "repost"
    rows = _rows(session, item.id)
    assert [r.step for r in rows] == list(STEPS), \
        "full ledger: flow ends at dedup; remaining steps recorded skipped"
    assert [r.status for r in rows[:3]] == ["succeeded", "succeeded", "succeeded"]
    assert all(r.status == "skipped" for r in rows[3:]), "no step beyond dedup executed"
    assert gateway.call_count() == 0, "no model stub call for a repost"


def test_unique_item_passes_dedup(session, orchestrator, tenant, gateway):
    item = make_item(session, tenant)
    result = orchestrator.process_item(item.id)
    assert result.status == "in_check"
    assert gateway.call_count() == len(MODEL_STEPS)


LONG_BODY = (
    "虚构情报正文：示例机构发布关于区域市场的观察声明，涉及渠道库存、价格带与"
    "终端动销的季度变化，供内部测试使用，不指向任何真实主体。"
) * 4  # long enough for stable shingle statistics


def test_near_duplicate_above_threshold_is_repost(session, orchestrator, tenant,
                                                  known_fingerprints):
    """Near-duplicate similarity detection: the seen set contains ONLY a lightly
    edited variant of the text (no exact match), and the shingle-Jaccard
    similarity (>= INTEL_DEDUP_SIMILARITY_THRESHOLD) still flags it as a repost."""
    item = make_item(session, tenant, payload=f"<p>{LONG_BODY}</p>")
    body = _normalized_body(item.payload)
    near = body.replace("渠道库存", "渠道备货", 1)  # lightly edited variant
    assert near != body, "seed must not accidentally be the exact text"
    known_fingerprints.add(near)  # near fingerprint ONLY — no exact fingerprint

    result = orchestrator.process_item(item.id)
    assert result.status == "discarded"
    assert result.last_error_code == "repost"


def test_below_threshold_similar_text_passes_dedup(session, orchestrator, tenant,
                                                   known_fingerprints, gateway):
    """A genuinely different text must NOT be dropped by the near-duplicate gate."""
    item = make_item(session, tenant, payload=f"<p>{LONG_BODY}</p>")
    body = _normalized_body(item.payload)
    other = "完全不同的一段虚构文本：今日天气晴朗，适合户外活动与采风记录。"
    assert other not in known_fingerprints
    known_fingerprints.add(other)

    result = orchestrator.process_item(item.id)
    assert result.status == "in_check", "dissimilar text passes the near-duplicate gate"
    assert gateway.call_count() == len(MODEL_STEPS)


def test_dedup_threshold_from_env(monkeypatch):
    import importlib
    import app.config as config

    monkeypatch.setenv("INTEL_DEDUP_SIMILARITY_THRESHOLD", "0.95")
    importlib.reload(config)
    assert config.DEDUP_SIMILARITY_THRESHOLD == 0.95
    monkeypatch.setenv("INTEL_DEDUP_SIMILARITY_THRESHOLD", "bogus")
    with pytest.raises(RuntimeError):
        importlib.reload(config)
    # restore the default for subsequent config consumers
    monkeypatch.delenv("INTEL_DEDUP_SIMILARITY_THRESHOLD")
    importlib.reload(config)
    assert config.DEDUP_SIMILARITY_THRESHOLD == 0.85


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
    assert all(r.status == "succeeded" for r in rows[: link_idx + 1])
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
    assert not [r for r in rows if r.status == "succeeded" and r.step != "extract"]
    assert gateway.call_count() == 0, "no downstream step ran"
