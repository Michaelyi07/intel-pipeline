"""Step implementations.

Per the ticket, dependencies on other tickets (model gateway, matching service) are
stubbed. Each step function has the same signature so the orchestrator can treat them
uniformly. Steps marked as model steps raise via the gateway client stub.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import STEPS


class LinkGateDrop(Exception):
    """Raised when link_check threshold rejects the item; flow ends at link_check."""

    error_code = "link_gate_drop"


class RepostDetected(Exception):
    """Raised when dedup judges the item a repost (exact or near-duplicate)."""

    error_code = "repost"


class GatewayUnavailableError(ConnectionError):
    """The model gateway is unreachable.

    Distinct from ordinary step failures: the orchestrator treats this as a
    queue-level pause condition (item stays resumable, no retry/backoff burn),
    per the ticket rule "the whole queue pauses rather than failing item by item".
    """

    error_code = "gateway_unavailable"


@dataclass
class StepContext:
    """State carried through the pipeline. `data` accumulates step outputs."""

    org_id: str
    site_id: str
    source_url: str
    raw_content: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Non-model steps (deterministic, no gateway call)
# ---------------------------------------------------------------------------

def extract_step(ctx: StepContext) -> None:
    # Body-text extraction from the captured page (stub: strip tags / keep main block)
    text = re.sub(r"<[^>]+>", "", ctx.raw_content).strip()
    if not text:
        raise ValueError("empty body after extract")
    ctx.data["body_text"] = text


def normalize_step(ctx: StepContext) -> None:
    # Normalize dates to ISO-8601 and organization names to canonical form (stub rules)
    text = ctx.data["body_text"]
    text = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", text)
    ctx.data["normalized_text"] = text
    ctx.data["organizations"] = sorted(set(re.findall(r"[\u4e00-\u9fa5]{2,8}(?:公司|集团|协会)", text)))


def dedup_step(ctx: StepContext) -> None:
    # Exact + near-duplicate detection against the seen-fingerprint set injected
    # into ctx.data["known_fingerprints"] by the caller (integration point: the
    # dedup ticket swaps in a real store behind the same set-like interface).
    #
    # Fingerprints store the normalized text of each seen item.
    # - Exact match: identical normalized text -> repost.
    # - Near-duplicate: character-shingle (5-gram) Jaccard similarity of the
    #   normalized texts at or above ctx.data["dedup_similarity_threshold"]
    #   (from INTEL_DEDUP_SIMILARITY_THRESHOLD, default 0.85) -> repost.
    fingerprints: set[str] = ctx.data.get("known_fingerprints", set())
    if not fingerprints:
        return
    text = ctx.data["normalized_text"]
    threshold = float(ctx.data.get("dedup_similarity_threshold", _DEFAULT_SIMILARITY_THRESHOLD))
    if text in fingerprints:
        raise RepostDetected()
    shingles = _shingles(text)
    for seen_text in fingerprints:
        if _jaccard(shingles, _shingles(seen_text)) >= threshold:
            raise RepostDetected()


# Character n-gram shingle Jaccard similarity for near-duplicate judgement.
_SHINGLE_K = 5
_DEFAULT_SIMILARITY_THRESHOLD = 0.85  # fallback; runtime value comes from config/env


def _shingles(text: str, k: int = _SHINGLE_K) -> set[str]:
    if not text:
        return set()
    if len(text) < k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def link_check_step(ctx: StepContext) -> None:
    # Original-link threshold: drop items that fail the minimum score (ticket rule).
    # Threshold default mirrors config.INTEL_LINK_CHECK_MIN_SCORE; kept inline because
    # steps must stay import-light for unit tests.
    score = int(ctx.data.get("link_score", 0))
    if score < ctx.data.get("link_check_min_score", 60):
        raise LinkGateDrop()
    ctx.data["link_verified"] = True


def build_checks_step(ctx: StepContext) -> None:
    # Numeric/date alignment: every number and date mentioned in the summary must
    # appear in the body text (stub: substring alignment check)
    summary = ctx.data.get("summary", "")
    body = ctx.data.get("body_text", "")
    missing = [tok for tok in re.findall(r"\d+", summary) if tok not in body]
    if missing:
        raise ValueError(f"number/date misalignment: {missing}")


def in_check_step(ctx: StepContext) -> None:
    # Final step: item enters the human verification queue (recorded as payload marker)
    ctx.data["entered_check_queue"] = True


# ---------------------------------------------------------------------------
# Model steps (call the gateway; auto-retry handled by the orchestrator)
# ---------------------------------------------------------------------------

def make_model_step(step_name: str, gateway: "ModelGateway") -> Callable[[StepContext], None]:
    def step(ctx: StepContext) -> None:
        result = gateway.invoke(step_name, ctx.data)
        ctx.data[step_name] = result

    step.__name__ = f"{step_name}_step"
    return step


class ModelGateway:
    """Integration stub for the model gateway ticket (documented in README)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self._unavailable = False

    def set_unavailable(self, unavailable: bool) -> None:
        self._unavailable = unavailable

    @property
    def is_unavailable(self) -> bool:
        return self._unavailable

    def invoke(self, step_name: str, data: dict[str, Any]) -> str:
        if self._unavailable:
            raise GatewayUnavailableError("model gateway unavailable")
        self.calls.append((step_name, len(self.calls)))
        return f"{step_name}-ok"

    def call_count(self, step_name: str | None = None) -> int:
        if step_name is None:
            return len(self.calls)
        return sum(1 for s, _ in self.calls if s == step_name)


def build_step_registry(gateway: ModelGateway) -> dict[str, Callable[[StepContext], None]]:
    registry: dict[str, Callable[[StepContext], None]] = {
        "extract": extract_step,
        "normalize": normalize_step,
        "dedup": dedup_step,
        "link_check": link_check_step,
        "redact": make_model_step("redact", gateway),
        "tag": make_model_step("tag", gateway),
        "summarize": make_model_step("summarize", gateway),
        "conditions": make_model_step("conditions", gateway),
        "build_checks": build_checks_step,
        "match": make_model_step("match", gateway),
        "in_check": in_check_step,
    }
    # Sanity: registry covers exactly the declared step order
    assert set(registry) == set(STEPS), "step registry must match STEPS order"
    return registry
