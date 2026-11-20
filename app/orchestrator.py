"""Pipeline orchestrator.

Rules implemented (ticket S5-INTEL-023):
- Steps run in declared order; each step writes an intel_item_steps row.
- A failed step stops the chain.
- Repost (dedup) or link-gate drop ends the flow at the corresponding step.
- Model steps auto-retry twice with 1 min / 5 min backoff; on final failure the
  item status becomes processing_failed.
- Reprocess resumes from the first non-succeeded step (failed/skipped ones rerun).
- Gateway-unavailable (GatewayUnavailableError) is NOT an ordinary failure: the
  item is returned to a resumable state (status pending, no retry burn, no
  skipped markers) and the orchestrator signals the worker to pause the queue
  via `pause_requested`.

The clock is injectable (`sleep` callable) so tests verify backoff without waiting.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .config import LINK_CHECK_MIN_SCORE, MODEL_RETRY_BACKOFF_SECONDS, MODEL_RETRY_MAX_ATTEMPTS
from .models import (
    MODEL_STEPS,
    STEPS,
    IntelItem,
    IntelItemStep,
    utcnow,
)
from .steps import (
    GatewayUnavailableError,
    LinkGateDrop,
    RepostDetected,
    StepContext,
)

logger = logging.getLogger("intel-pipeline.orchestrator")


class Orchestrator:
    def __init__(
        self,
        session: Session,
        step_registry: dict[str, Callable[[StepContext], None]],
        sleep: Callable[[float], None] = time.sleep,
        known_fingerprints: set[str] | None = None,
    ) -> None:
        self.session = session
        self.registry = step_registry
        self.sleep = sleep
        # Set while processing when the gateway turns unavailable mid-item; the
        # worker reads this after each batch to pause the queue and requeue the
        # affected item (kept resumable, never counted as failed).
        self.pause_requested = False
        # Explicit None check: `set() or set()` would silently copy and break identity
        # for an empty seeded set (tests seed fingerprints after construction).
        self.known_fingerprints = known_fingerprints if known_fingerprints is not None else set()

    # ------------------------------------------------------------------
    # Step record helpers
    # ------------------------------------------------------------------
    def _record_step(
        self,
        item: IntelItem,
        step: str,
        status: str,
        attempt: int,
        error_code: str | None,
        duration_ms: int,
    ) -> IntelItemStep:
        row = IntelItemStep(
            item_id=item.id,
            org_id=item.org_id,
            site_id=item.site_id,
            step=step,
            status=status,
            attempt=attempt,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        self.session.add(row)
        return row

    def _first_pending_step(self, item_id: uuid.UUID) -> int:
        """Index of the first step that is not yet successful (resume point)."""
        rows = self.session.execute(
            select(IntelItemStep)
            .where(
                IntelItemStep.item_id == item_id,
                IntelItemStep.deleted_at.is_(None),
            )
            .order_by(IntelItemStep.created_at, IntelItemStep.id)
        ).scalars().all()
        succeeded = {r.step for r in rows if r.status == "succeeded"}
        for idx, step in enumerate(STEPS):
            if step not in succeeded:
                return idx
        return len(STEPS)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def process_item(self, item_id: uuid.UUID) -> IntelItem:
        item = self.session.get(IntelItem, item_id)
        if item is None or item.deleted_at is not None:
            raise ValueError(f"intel item {item_id} not found")

        self.pause_requested = False
        if item.status in ("processing_failed", "pending", "processing"):
            start_idx = self._first_pending_step(item_id)
        elif item.status == "in_check":
            return item
        else:  # discarded stays terminal
            return item

        item.status = "processing"
        ctx = StepContext(
            org_id=str(item.org_id),
            site_id=str(item.site_id),
            source_url=item.source_url,
            raw_content=item.payload or "",
        )
        ctx.data["known_fingerprints"] = self.known_fingerprints
        ctx.data["link_score"] = item.link_score
        ctx.data["link_check_min_score"] = LINK_CHECK_MIN_SCORE
        # Runtime read (not import-time) so env-var overrides via reload work in tests.
        ctx.data["dedup_similarity_threshold"] = config.DEDUP_SIMILARITY_THRESHOLD

        for idx in range(start_idx, len(STEPS)):
            step_name = STEPS[idx]
            is_model_step = step_name in MODEL_STEPS
            max_attempts = MODEL_RETRY_MAX_ATTEMPTS if is_model_step else 1
            attempt = 0
            step_done = False

            while attempt < max_attempts and not step_done:
                attempt += 1
                started = utcnow()
                try:
                    self.registry[step_name](ctx)
                    duration_ms = int((utcnow() - started).total_seconds() * 1000)
                    self._record_step(item, step_name, "succeeded", attempt, None, duration_ms)
                    step_done = True
                except RepostDetected as exc:
                    duration_ms = int((utcnow() - started).total_seconds() * 1000)
                    self._record_step(item, step_name, "succeeded", attempt, None, duration_ms)
                    self._mark_skipped(item, STEPS[idx + 1 :])
                    item.status = "discarded"
                    item.last_error_code = exc.error_code
                    self.session.commit()
                    logger.info("repost item=%s ended at step=%s", item.id, step_name)
                    return item
                except LinkGateDrop as exc:
                    duration_ms = int((utcnow() - started).total_seconds() * 1000)
                    self._record_step(item, step_name, "succeeded", attempt, None, duration_ms)
                    self._mark_skipped(item, STEPS[idx + 1 :])
                    item.status = "discarded"
                    item.last_error_code = exc.error_code
                    self.session.commit()
                    logger.info("link gate drop item=%s at step=%s", item.id, step_name)
                    return item
                except GatewayUnavailableError as exc:
                    # Ticket rule: gateway unavailability pauses the whole queue,
                    # it is not a per-item failure. Keep the item resumable
                    # (status pending, completed steps stay succeeded, no skipped
                    # markers, no retry/backoff burn) and let the worker pause.
                    duration_ms = int((utcnow() - started).total_seconds() * 1000)
                    self._record_step(item, step_name, "failed", attempt, exc.error_code, duration_ms)
                    item.status = "pending"
                    item.last_error_code = exc.error_code
                    self.pause_requested = True
                    self.session.commit()
                    logger.info(
                        "gateway unavailable item=%s at step=%s; queue pause requested",
                        item.id, step_name,
                    )
                    return item
                except Exception as exc:  # noqa: BLE001 — step boundary, classify below
                    duration_ms = int((utcnow() - started).total_seconds() * 1000)
                    error_code = type(exc).__name__[:64]
                    self._record_step(item, step_name, "failed", attempt, error_code, duration_ms)
                    self.session.commit()
                    if attempt < max_attempts and is_model_step:
                        backoff = MODEL_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(MODEL_RETRY_BACKOFF_SECONDS) - 1)]
                        logger.info(
                            "retry item=%s step=%s attempt=%d backoff=%ds",
                            item.id, step_name, attempt, backoff,
                        )
                        self.sleep(backoff)
                    else:
                        item.status = "processing_failed"
                        item.last_error_code = error_code
                        self._mark_skipped(item, STEPS[idx + 1 :])
                        self.session.commit()
                        logger.info(
                            "item=%s processing_failed at step=%s attempts=%d",
                            item.id, step_name, attempt,
                        )
                        return item

            if step_name == "in_check":
                item.status = "in_check"
                item.last_error_code = None
                item.payload = json_dumps(ctx.data)
                self.session.commit()
                logger.info("item=%s reached in_check", item.id)
                return item

        return item

    def _mark_skipped(self, item: IntelItem, remaining: tuple[str, ...] | list[str]) -> None:
        for step_name in remaining:
            self._record_step(item, step_name, "skipped", 0, None, 0)


def json_dumps(data: dict) -> str:
    import json

    return json.dumps({k: v for k, v in data.items() if k != "known_fingerprints"},
                      ensure_ascii=False, default=str)
