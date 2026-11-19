"""One-command demo: run a fictional entry through the pipeline and print the ledger.

Uses an in-memory SQLite-compatible flow only when DATABASE_URL is unset? No —
this demo requires a real PostgreSQL (DATABASE_URL), consistent with the tests.
It prints the full intel_item_steps ledger for visual inspection.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, IntelItem, IntelItemStep
from app.orchestrator import Orchestrator
from app.steps import ModelGateway, build_step_registry

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    url = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/intel_pipeline_test")
    eng = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, expire_on_commit=False)()

    gateway = ModelGateway()
    orch = Orchestrator(session, build_step_registry(gateway), sleep=lambda s: None)

    org_id, site_id = uuid.uuid4(), uuid.uuid4()
    item = IntelItem(
        org_id=org_id,
        site_id=site_id,
        source_url="https://example.internal/news/fictional-entry-demo",
        title="虚构演示条目",
        payload="<p>这是虚构的测试情报正文，日期 2026年11月19日，机构：示例协会。</p>",
        link_score=90,
        created_by="demo",
    )
    session.add(item)
    session.commit()

    result = orch.process_item(item.id)
    print(f"demo item {item.id} -> final status: {result.status}")
    rows = session.execute(
        select(IntelItemStep)
        .where(IntelItemStep.item_id == item.id)
        .order_by(IntelItemStep.created_at, IntelItemStep.id)
    ).scalars().all()
    for r in rows:
        print(f"  {r.step:<12} {r.status:<8} attempt={r.attempt} err={r.error_code} dur={r.duration_ms}ms")

    session.close()
    eng.dispose()


if __name__ == "__main__":
    main()
