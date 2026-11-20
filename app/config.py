"""Centralized configuration — everything from environment variables, nothing hardcoded."""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"env var {name} must be an integer, got {raw!r}") from None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"env var {name} must be a float, got {raw!r}") from None
    if not 0.0 < value <= 1.0:
        raise RuntimeError(f"env var {name} must be in (0, 1], got {raw!r}")
    return value


# PostgreSQL 16 connection URL (psycopg 3 driver)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test",
)

# Redis URL for the processing queue
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# Worker concurrency is controlled via environment variables (ticket requirement)
WORKER_CONCURRENCY = _int_env("INTEL_WORKER_CONCURRENCY", 2)

# Model gateway endpoint; the gateway itself is out of scope and stubbed in tests
MODEL_GATEWAY_URL = os.getenv("MODEL_GATEWAY_URL", "http://127.0.0.1:9000")
MODEL_GATEWAY_TIMEOUT_SECONDS = _int_env("MODEL_GATEWAY_TIMEOUT_SECONDS", 10)

# Auto-retry for model steps: 2 retries with 1 min / 5 min backoff (ticket requirement)
MODEL_RETRY_MAX_ATTEMPTS = _int_env("MODEL_RETRY_MAX_ATTEMPTS", 3)  # 1 initial + 2 retries
MODEL_RETRY_BACKOFF_SECONDS = (60, 300)

# Queue-level pause when the gateway is unavailable (ticket requirement)
QUEUE_PAUSED_KEY = "intel:queue:paused"

# Link_check threshold: item without a qualifying original link is dropped at that step
LINK_CHECK_MIN_SCORE = _int_env("INTEL_LINK_CHECK_MIN_SCORE", 60)

# Near-duplicate (dedup) similarity threshold: shingle-Jaccard similarity at or
# above this value counts as a repost. Read from the environment per ticket rule.
DEDUP_SIMILARITY_THRESHOLD = _float_env("INTEL_DEDUP_SIMILARITY_THRESHOLD", 0.85)
