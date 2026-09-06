"""Enforce one analysis row per (meeting_id, kind).

Revision ID: a1b2c3d4e5f6
Revises: f3a9c7e5b2d4
Create Date: 2026-09-05 18:00:00.000000

Additive and idempotent. The analysis write path is a select-then-insert
upsert, but without a database-level guarantee a race (or a legacy DB that
accumulated rows) could hold more than one row per (meeting, kind); readers
then pick an arbitrary one via ``scalar()``.

Pre-existing duplicates are collapsed first, keeping the most recently
modified row per (meeting_id, kind) -- the newest analysis is the one the
user expects to see. Ties break on ``id`` (deterministic). The unique index
is then created only if it is not there yet.
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f3a9c7e5b2d4"
branch_labels = None
depends_on = None


def _index_exists(bind, name: str) -> bool:
    row = bind.execute(sa.text(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :n"),
        {"n": name}).first()
    return row is not None


def _dedup_analysis(bind) -> None:
    # Keep the most recently modified analysis per (meeting_id, kind).
    bind.execute(sa.text(
        "DELETE FROM analysis WHERE id NOT IN ("
        "  SELECT keep_id FROM (SELECT id AS keep_id, ROW_NUMBER() OVER ("
        "    PARTITION BY meeting_id, kind "
        "    ORDER BY updated_at DESC, id DESC"
        "  ) AS rn FROM analysis) WHERE rn = 1)"))


def upgrade() -> None:
    bind = op.get_bind()
    _dedup_analysis(bind)
    if not _index_exists(bind, "uq_analysis_meeting_kind"):
        op.create_index("uq_analysis_meeting_kind", "analysis",
                        ["meeting_id", "kind"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "uq_analysis_meeting_kind"):
        op.drop_index("uq_analysis_meeting_kind", table_name="analysis")
