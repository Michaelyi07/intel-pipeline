"""Shared fixtures: real PostgreSQL 16 (no SQLite fallback) + fakeredis for the queue.

Database URL comes from DATABASE_URL (see .env.example / CI). Each test gets a clean
schema: tables are dropped and recreated per session, rows truncated per test.
"""

from __future__ import annotations

import os
import uuid

import fakeredis
import pytest
from sqlalchemy import create_engine, text

from app.models import Base, IntelItem
from app.orchestrator import Orchestrator
from app.steps import ModelGateway, build_step_registry
from app.worker import IntelQueue, Worker


TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE intel_item_steps, intel_items CASCADE"))


@pytest.fixture
def session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def tenant() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
def gateway() -> ModelGateway:
    return ModelGateway()


@pytest.fixture
def registry(gateway):
    return build_step_registry(gateway)


@pytest.fixture
def known_fingerprints():
    return set()


@pytest.fixture
def orchestrator(session, registry, known_fingerprints):
    return Orchestrator(session, registry, sleep=lambda s: None,
                        known_fingerprints=known_fingerprints)


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def queue(redis_client):
    return IntelQueue(redis_client)


@pytest.fixture
def worker(queue, orchestrator, gateway):
    return Worker(queue, lambda: orchestrator, gateway, concurrency=2)


def make_item(session, tenant, payload="<p>虚构测试情报正文 FICT-001</p>",
              source_url="https://example.internal/news/fict-001",
              link_score=90) -> IntelItem:
    org_id, site_id = tenant
    item = IntelItem(
        org_id=uuid.UUID(org_id),
        site_id=uuid.UUID(site_id),
        source_url=source_url,
        payload=payload,
        link_score=link_score,
        status="pending",
    )
    session.add(item)
    session.commit()
    return item
