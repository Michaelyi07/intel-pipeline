-- Equivalent plain-SQL DDL for teams applying by psql.
-- Alembic revision: 20261119_iip_001 (same schema).

CREATE TABLE intel_items (
    id UUID PRIMARY KEY,
    source_url TEXT NOT NULL,
    title TEXT,
    link_score INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    last_error_code VARCHAR(64),
    payload TEXT,
    org_id UUID NOT NULL,
    site_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_intel_items_org_id ON intel_items (org_id);
CREATE INDEX ix_intel_items_site_id ON intel_items (site_id);
CREATE INDEX ix_intel_items_status ON intel_items (status);

CREATE TABLE intel_item_steps (
    id UUID PRIMARY KEY,
    item_id UUID NOT NULL REFERENCES intel_items (id) ON DELETE CASCADE,
    step VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    error_code VARCHAR(64),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    org_id UUID NOT NULL,
    site_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_intel_item_steps_item_id ON intel_item_steps (item_id);
CREATE INDEX ix_intel_item_steps_org_id ON intel_item_steps (org_id);
CREATE INDEX ix_intel_item_steps_site_id ON intel_item_steps (site_id);
CREATE INDEX ix_intel_item_steps_step ON intel_item_steps (step);
