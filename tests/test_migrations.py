"""Migration acceptance: the Alembic revision applies on an empty database,
supports a real business case on the migrated schema, and downgrades cleanly.
(The revision builds fresh Column objects per table — no shared instances.)"""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.models import STEPS, Base, IntelItemStep
from app.orchestrator import Orchestrator
from app.steps import ModelGateway, build_step_registry
from tests.conftest import TEST_DATABASE_URL, make_item

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_config() -> Config:
    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations", "alembic"))
    return cfg


def test_migration_upgrade_business_case_downgrade(engine, session_factory, tenant, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)  # consumed by migrations/alembic/env.py
    cfg = _alembic_config()

    # Start from an empty schema: drop ORM-created tables and any stale alembic_version
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    try:
        command.upgrade(cfg, "head")

        # Business case runs on the MIGRATED schema (not Base.metadata.create_all)
        session = session_factory()
        gateway = ModelGateway()
        orch = Orchestrator(session, build_step_registry(gateway), sleep=lambda _s: None)
        item = make_item(session, tenant)
        result = orch.process_item(item.id)
        assert result.status == "in_check"
        rows = session.execute(
            select(IntelItemStep).where(
                IntelItemStep.item_id == item.id, IntelItemStep.deleted_at.is_(None)
            ).order_by(IntelItemStep.created_at, IntelItemStep.id)
        ).scalars().all()
        assert [r.step for r in rows] == list(STEPS)
        assert all(r.status == "succeeded" for r in rows)
        session.close()

        # Downgrade removes both tables
        command.downgrade(cfg, "base")
        with engine.begin() as conn:
            remaining = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('intel_items', 'intel_item_steps')"
            )).scalar()
        assert remaining == 0, "downgrade base drops both tables"
    finally:
        # Restore the ORM-managed schema for the other tests (clean_tables truncates it)
        Base.metadata.create_all(engine)
