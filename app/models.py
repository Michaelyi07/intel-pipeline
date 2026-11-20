"""SQLAlchemy 2 models for the intelligence entry processing pipeline.

All tables follow the platform's unified technical conventions:
id (UUID), org_id, site_id, created_at, created_by, updated_at, deleted_at (soft delete).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # UTC everywhere; local conversion is a display concern (engineering standard 3.7 #18)
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntelItem(Base, TimestampMixin):
    """A captured page entering the processing pipeline."""

    __tablename__ = "intel_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Link threshold score provided by the crawler ticket (integration point);
    # link_check drops the item when it is below the configured minimum.
    link_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # pending | processing | in_check | processing_failed | discarded
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    # set when the flow ends early: repost / link_gate_drop / processing_failed
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON blob of step outputs


class IntelItemStep(Base, TimestampMixin):
    """One row per step execution attempt. The pipeline's observable record."""

    __tablename__ = "intel_item_steps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("intel_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    step: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # succeeded | failed | skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


STEPS: tuple[str, ...] = (
    "extract",
    "normalize",
    "dedup",
    "link_check",
    "redact",
    "tag",
    "summarize",
    "conditions",
    "build_checks",
    "match",
    "in_check",
)

# Steps that call the model gateway; subject to auto-retry with backoff.
MODEL_STEPS: frozenset[str] = frozenset({"redact", "tag", "summarize", "conditions", "match"})

ITEM_TERMINAL_STATUSES: frozenset[str] = frozenset({"in_check", "processing_failed", "discarded"})
