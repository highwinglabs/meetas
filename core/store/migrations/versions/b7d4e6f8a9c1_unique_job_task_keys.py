"""Add unique keys for job and task dedup (smart merge of pre-existing dups).

Additive and idempotent. Adds a unique index to ``task(dedup_key)`` and
``processing_job(meeting_id, stage)`` so the query-then-insert dedup paths
cannot produce duplicate rows under concurrency.

Pre-existing duplicates are collapsed with a *semantic* winner selection rather
than "keep the earliest row", so the survivor represents the true current state:

* Jobs (per meeting_id + stage): the most advanced status wins,
  ``done > running > pending > failed > cancelled``. A completed job is never
  masked by a stale ``pending``/``failed`` duplicate. Ties break on the most
  recent ``updated_at`` then ``id`` (deterministic).
* Tasks (per non-null dedup_key): the most recently modified row wins
  (latest ``updated_at`` then ``id``), so a manually edited task is preserved
  over an un-edited duplicate.

User data (meetings, transcripts, analyses, backups) is never touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "b7d4e6f8a9c1"
down_revision = "a2c7e9f4b1d6"
branch_labels = None
depends_on = None


def _index_exists(bind, name: str) -> bool:
    row = bind.execute(sa.text(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :n"),
        {"n": name}).first()
    return row is not None


# Job-status priority: higher = more advanced / more important to keep.
_JOB_STATUS_PRIORITY = (
    "CASE status "
    "WHEN 'done' THEN 4 "
    "WHEN 'running' THEN 3 "
    "WHEN 'pending' THEN 2 "
    "WHEN 'failed' THEN 1 "
    "WHEN 'cancelled' THEN 0 "
    "ELSE -1 END"
)


def _dedup_jobs(bind) -> None:
    # Keep the most-advanced job per (meeting_id, stage); drop the rest.
    bind.execute(sa.text(
        "DELETE FROM processing_job WHERE id NOT IN ("
        "  SELECT keep_id FROM (SELECT id AS keep_id, ROW_NUMBER() OVER ("
        f"    PARTITION BY meeting_id, stage "
        f"    ORDER BY {_JOB_STATUS_PRIORITY} DESC, updated_at DESC, id DESC"
        "  ) AS rn FROM processing_job) WHERE rn = 1)"))


def _dedup_tasks(bind) -> None:
    # Keep the most recently modified task per non-null dedup_key; drop the rest.
    bind.execute(sa.text(
        "DELETE FROM task WHERE dedup_key IS NOT NULL AND id NOT IN ("
        "  SELECT keep_id FROM (SELECT id AS keep_id, ROW_NUMBER() OVER ("
        "    PARTITION BY dedup_key ORDER BY updated_at DESC, id DESC"
        "  ) AS rn FROM task WHERE dedup_key IS NOT NULL) WHERE rn = 1)"))


def upgrade() -> None:
    bind = op.get_bind()
    _dedup_tasks(bind)
    _dedup_jobs(bind)
    if not _index_exists(bind, "uq_task_dedup_key"):
        op.create_index("uq_task_dedup_key", "task", ["dedup_key"], unique=True)
    if not _index_exists(bind, "uq_processing_job"):
        op.create_index("uq_processing_job", "processing_job",
                        ["meeting_id", "stage"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "uq_processing_job"):
        op.drop_index("uq_processing_job", table_name="processing_job")
    if _index_exists(bind, "uq_task_dedup_key"):
        op.drop_index("uq_task_dedup_key", table_name="task")
