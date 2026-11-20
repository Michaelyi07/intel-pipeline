"""Redis-backed queue worker.

Ticket rules:
- The queue uses Redis; worker concurrency is controlled via environment variables.
- When the model gateway is unavailable, the whole queue pauses (no items pulled,
  no per-item failures). Items that hit the outage mid-processing are requeued
  in a resumable state and the queue pauses — they are never counted as failed.
- Recovery is automatic: each cycle the worker mirrors gateway health into the
  Redis pause flag in BOTH directions — an outage sets it, a healthy gateway
  clears it — so processing resumes on the next cycle without manual action
  (see ADR-002).
- Worker concurrency is real parallelism: up to `concurrency` items are
  processed concurrently in a thread pool (each task gets its own orchestrator
  from the injected factory, so each thread owns its DB session).
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
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

    def push_front(self, item_id: uuid.UUID) -> None:
        # Requeue at the head (FIFO position preserved for the affected batch).
        self.client.lpush(self.key, str(item_id))

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
    pulling (queue-level pause) instead of failing items one by one, and an
    outage discovered mid-item requeues the affected item and pauses the queue.
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
        # worker processes observe the same queue-level state — in both
        # directions: outage pauses, recovery auto-clears the flag.
        if self.gateway.is_unavailable:
            self.queue.pause()
        elif self.queue.is_paused():
            self.queue.resume()

    def _process_one(self, item_id: uuid.UUID) -> tuple[Orchestrator, object]:
        orch = self.orchestrator_factory()
        item = orch.process_item(item_id)
        return orch, item

    def run_once(self) -> dict:
        """Process up to `concurrency` items concurrently. Returns a summary dict.

        When the gateway is unavailable: nothing is pulled, nothing fails;
        the queue pause flag is set and items stay in the queue.
        When the gateway turns unavailable mid-batch: finished items count
        normally, the affected items are requeued (resumable, not failed) and
        the queue pauses.
        """
        self._refresh_pause_flag()
        if self.gateway.is_unavailable or self.queue.is_paused():
            logger.info("queue paused (gateway unavailable), %d items waiting", self.queue.depth())
            return {"paused": True, "processed": 0, "failed": 0}

        item_ids: list[uuid.UUID] = []
        for _ in range(self.concurrency):
            item_id = self.queue.pop()
            if item_id is None:
                break
            item_ids.append(item_id)
        if not item_ids:
            return {"paused": False, "processed": 0, "failed": 0}

        processed = 0
        failed = 0
        to_requeue: list[uuid.UUID] = []
        with ThreadPoolExecutor(max_workers=len(item_ids)) as pool:
            for orch, item in pool.map(self._process_one, item_ids):
                if getattr(orch, "pause_requested", False):
                    to_requeue.append(item.id)
                elif item.status == "processing_failed":
                    failed += 1
                else:
                    processed += 1

        if to_requeue:
            for item_id in reversed(to_requeue):
                self.queue.push_front(item_id)
            self.queue.pause()
            logger.info(
                "gateway unavailable mid-batch: %d item(s) requeued, queue paused",
                len(to_requeue),
            )
            return {"paused": True, "processed": processed, "failed": failed}
        return {"paused": False, "processed": processed, "failed": failed}

    def run_until_empty(self, max_rounds: int = 100) -> dict:
        totals = {"paused": False, "processed": 0, "failed": 0}
        for _ in range(max_rounds):
            if self.queue.depth() == 0:
                break
            # run_once refreshes the pause flag first: a healthy gateway
            # auto-clears a stale pause, so a recovered queue resumes here
            # without any manual flag clear.
            result = self.run_once()
            totals["processed"] += result["processed"]
            totals["failed"] += result["failed"]
            if result["paused"]:
                totals["paused"] = True
                break
        return totals
