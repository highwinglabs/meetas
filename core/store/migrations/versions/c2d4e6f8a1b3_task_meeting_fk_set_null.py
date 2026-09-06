"""Change Task.meeting_id FK action from ON DELETE CASCADE to ON DELETE SET NULL.

Revision ID: c2d4e6f8a1b3
Revises: a1b2c3d4e5f6
Create Date: 2026-09-05 20:00:00.000000

Design rationale (L6): ``Task.meeting_id`` is nullable on purpose so a task can
be promoted to the global overview and outlive its meeting. The ``project_id``
FK already uses ``SET NULL``; ``meeting_id`` used ``CASCADE``, so permanently
deleting a meeting silently destroyed its tasks -- contradicting the nullable,
first-class-task design.

SQLite cannot alter a foreign-key action in place, so the table is rebuilt:
copy every current column verbatim into a new table whose ``meeting_id`` FK uses
``SET NULL``, then swap the names and recreate the indexes. The column set is
read live from the existing table (not hard-coded) so the rebuild always copies
exactly the columns present, and every index is recreated afterwards.

The migration engine does not enable ``PRAGMA foreign_keys`` (the app's runtime
engine does, but the Alembic connection does not), so the ``DROP TABLE`` does not
cascade into ``task_history``. A defensive ``PRAGMA foreign_keys=OFF``/``ON`` pair
documents the intent; it is a harmless no-op inside the migration transaction.

Idempotent: if the ``meeting_id`` FK already reports ``SET NULL`` the rebuild is
skipped entirely.
"""
from alembic import op
import sqlalchemy as sa


revision = "c2d4e6f8a1b3"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _meeting_fk_on_delete(bind) -> str | None:
    """Return the ON DELETE action of task.meeting_id -> meeting.id, or None."""
    rows = bind.execute(sa.text('PRAGMA foreign_key_list("task")')).fetchall()
    for r in rows:
        # PRAGMA foreign_key_list columns:
        # (id, seq, table, from, to, on_update, on_delete, match)
        if r[2] == "meeting" and r[3] == "meeting_id":
            return r[6]
    return None


def _rebuild_meeting_fk(bind, on_delete: str) -> None:
    """Rebuild the task table so the meeting_id FK uses ``on_delete``."""
    insp = sa.inspect(bind)
    columns = insp.get_columns("task")
    if not columns:
        return
    col_defs = []
    for c in columns:
        nullable = "" if c["nullable"] else " NOT NULL"
        col_defs.append(f'"{c["name"]}" {c["type"]}{nullable}')
    col_defs.append('PRIMARY KEY ("id")')
    col_defs.append(
        f'FOREIGN KEY("meeting_id") REFERENCES "meeting"("id") '
        f"ON DELETE {on_delete}")
    col_defs.append(
        'FOREIGN KEY("project_id") REFERENCES "project"("id") ON DELETE SET NULL')

    bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    bind.execute(sa.text("DROP TABLE IF EXISTS task_new"))
    bind.execute(sa.text(
        "CREATE TABLE task_new (" + ", ".join(col_defs) + ")"))
    names = ", ".join(f'"{c["name"]}"' for c in columns)
    bind.execute(sa.text(
        f"INSERT INTO task_new ({names}) SELECT {names} FROM task"))
    bind.execute(sa.text("DROP TABLE task"))
    bind.execute(sa.text("ALTER TABLE task_new RENAME TO task"))

    # Recreate every index the task table had (dropped along with the old table).
    for name, cols, unique in (
        ("ix_task_archived_at", ("archived_at",), False),
        ("ix_task_dedup_key", ("dedup_key",), False),
        ("ix_task_deleted_at", ("deleted_at",), False),
        ("ix_task_meeting_id", ("meeting_id",), False),
        ("ix_task_project_id", ("project_id",), False),
        ("ix_task_status", ("status",), False),
        ("uq_task_dedup_key", ("dedup_key",), True),
    ):
        col_sql = ", ".join(f'"{c}"' for c in cols)
        bind.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
        bind.execute(sa.text(
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON task ({col_sql})"))
    bind.execute(sa.text("PRAGMA foreign_keys=ON"))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "task" not in insp.get_table_names():
        return
    if _meeting_fk_on_delete(bind) == "SET NULL":
        return
    _rebuild_meeting_fk(bind, "SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "task" not in insp.get_table_names():
        return
    if _meeting_fk_on_delete(bind) == "CASCADE":
        return
    _rebuild_meeting_fk(bind, "CASCADE")
