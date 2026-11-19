"""Redis-backed queue worker.

Ticket rules:
- The queue uses Redis; worker concurrency is controlled via environment variables.
- When the model gateway is unavailable, the whole queue pauses (no items pulled,
  no per-item failures). Resuming requires clearing the Redis pause flag once the
  gateway is healthy again (see ADR-002: the flag clear is a deliberate manual /
  health-probe-driven action, not automatic).
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

import redis as redis_lib

from .config import QUEUE_PAUSED_KEY, WORKER_CONCURRENCY
from .orchestrator import Orchestrator
from .steps import ModelGateway

logger = logging.getLogger("intel-pipeline.worker")


class IntelQueue:
    """Thin Redis wrapper around a FIFO list of item ids."""

    def __init__(self, client: "redis_lib.Redis", key: str = "intel:queue:items") -> None:
        self.client = client
        self.key = key

    def enqueue(self, item_id: uuid.UUID) -> None:
        self.client.rpush(self.key, str(item_id))

    def pop(self) -> uuid.UUID | None:
        raw = self.client.lpop(self.key)
        return uuid.UUID(raw) if raw else None

    def depth(self) -> int:
        return int(self.client.llen(self.key))

    def is_paused(self) -> bool:
        return bool(self.client.get(QUEUE_PAUSED_KEY))

    def pause(self) -> None:
        self.client.set(QUEUE_PAUSED_KEY, "1")

    def resume(self) -> None:
        self.client.delete(QUEUE_PAUSED_KEY)


def make_redis_client(url: str):
    return redis_lib.Redis.from_url(url, decode_responses=True)


class Worker:
    """Pulls items from the queue and runs the orchestrator.

    Gateway health gates the whole queue: when unavailable, the worker stops
    pulling (queue-level pause) instead of failing items one by one.
    """

    def __init__(
        self,
        queue: IntelQueue,
        orchestrator_factory: Callable[[], Orchestrator],
        gateway: ModelGateway,
        concurrency: int = WORKER_CONCURRENCY,
    ) -> None:
        self.queue = queue
        self.orchestrator_factory = orchestrator_factory
        self.gateway = gateway
        self.concurrency = max(1, concurrency)

    def _refresh_pause_flag(self) -> None:
        # Gateway state is mirrored into the Redis pause flag so any number of
        # worker processes observe the same queue-level state.
        if self.gateway.is_unavailable:
            self.queue.pause()

    def run_once(self) -> dict:
        """Process up to `concurrency` items. Returns a summary dict.

        When the gateway is unavailable: nothing is pulled, nothing fails;
        the queue pause flag is set and items stay in the queue.
        """
        self._refresh_pause_flag()
        if self.gateway.is_unavailable or self.queue.is_paused():
            logger.info("queue paused (gateway unavailable), %d items waiting", self.queue.depth())
            return {"paused": True, "processed": 0, "failed": 0}

        processed = 0
        failed = 0
        for _ in range(self.concurrency):
            item_id = self.queue.pop()
            if item_id is None:
                break
            orch = self.orchestrator_factory()
            item = orch.process_item(item_id)
            if item.status == "processing_failed":
                failed += 1
            else:
                processed += 1
        return {"paused": False, "processed": processed, "failed": failed}

    def run_until_empty(self, max_rounds: int = 100) -> dict:
        totals = {"paused": False, "processed": 0, "failed": 0}
        for _ in range(max_rounds):
            if self.queue.is_paused() or self.gateway.is_unavailable:
                totals["paused"] = True
                break
            if self.queue.depth() == 0:
                break
            result = self.run_once()
            totals["processed"] += result["processed"]
            totals["failed"] += result["failed"]
        return totals
