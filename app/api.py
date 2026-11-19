"""FastAPI routes for the workbench integration.

POST /api/intel/items/{id}/reprocess — manual re-run starting from the failed step.
Tenant scoping: org_id/site_id come from trusted gateway headers (integration point
documented in README); every query is scoped by them to prevent cross-tenant access.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select

from .models import IntelItem
from .orchestrator import Orchestrator
from .steps import build_step_registry, ModelGateway

router = APIRouter(prefix="/api/intel")


def _tenant_ids(org_id: str | None, site_id: str | None) -> tuple[uuid.UUID, uuid.UUID]:
    if not org_id or not site_id:
        raise HTTPException(status_code=400, detail="missing X-Org-Id / X-Site-Id headers")
    try:
        return uuid.UUID(org_id), uuid.UUID(site_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid tenant id format") from exc


@router.post("/items/{item_id}/reprocess")
def reprocess_item(
    item_id: uuid.UUID,
    x_org_id: str | None = Header(default=None),
    x_site_id: str | None = Header(default=None),
) -> dict:
    org, site = _tenant_ids(x_org_id, x_site_id)

    # Session/registry are injected per request in the real app factory (see README);
    # reprocess is exposed as a callable service so tests drive it with the same
    # code path the API uses.
    raise_or_run = ReprocessService.run
    return raise_or_run(item_id, org, site)


class ReprocessService:
    """Indirection point so tests and the API share one implementation."""

    orchestrator: Orchestrator | None = None

    @staticmethod
    def run(item_id: uuid.UUID, org: uuid.UUID, site: uuid.UUID) -> dict:
        orch = ReprocessService.orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not configured")
        session = orch.session
        item = session.get(IntelItem, item_id)
        # Tenant-scoped lookup: a cross-tenant item is indistinguishable from a
        # missing one (404) — closes the IDOR path flagged in review.
        if item is None or item.deleted_at is not None or item.org_id != org or item.site_id != site:
            raise HTTPException(status_code=404, detail="item not found")
        if item.status not in ("processing_failed", "pending"):
            raise HTTPException(status_code=409, detail=f"item not reprocessable (status={item.status})")

        result = orch.process_item(item_id)
        return {"id": str(item_id), "status": result.status}


def make_app_orchestrator(session, gateway: ModelGateway) -> Orchestrator:
    return Orchestrator(session, build_step_registry(gateway))
