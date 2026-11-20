"""intel_item_steps + intel_items — real Alembic revision (upgrade/downgrade).

Revision ID: 20261119_iip_001
Revises: -
Create Date: 2026-11-19
"""

from alembic import op
import sqlalchemy as sa

revision = "20261119_iip_001"
down_revision = None
branch_labels = None
depends_on = None

def _unified_columns() -> list:
    """Fresh Column instances per call.

    A sa.Column object must not be reused across op.create_table calls —
    sharing one instance between tables corrupts the second table's DDL.
    """
    return [
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "intel_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("link_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        *_unified_columns(),
    )
    op.create_index("ix_intel_items_org_id", "intel_items", ["org_id"])
    op.create_index("ix_intel_items_site_id", "intel_items", ["site_id"])
    op.create_index("ix_intel_items_status", "intel_items", ["status"])

    op.create_table(
        "intel_item_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Uuid(),
            sa.ForeignKey("intel_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        *_unified_columns(),
    )
    op.create_index("ix_intel_item_steps_item_id", "intel_item_steps", ["item_id"])
    op.create_index("ix_intel_item_steps_org_id", "intel_item_steps", ["org_id"])
    op.create_index("ix_intel_item_steps_site_id", "intel_item_steps", ["site_id"])
    op.create_index("ix_intel_item_steps_step", "intel_item_steps", ["step"])


def downgrade() -> None:
    op.drop_table("intel_item_steps")
    op.drop_table("intel_items")
