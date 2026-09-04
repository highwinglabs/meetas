"""Add recoverable archive and trash metadata to tasks."""
from alembic import op
import sqlalchemy as sa


revision = "c9e8f1a2b3d4"
down_revision = "b7d4e6f8a9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("task")}
    # Legacy databases may have been created with the current ORM via
    # create_all before Alembic was enabled. Keep this additive migration
    # idempotent for those databases as well as normal upgrade paths.
    if "archived_at" not in columns:
        op.add_column("task", sa.Column("archived_at", sa.DateTime(), nullable=True))
    if "deleted_at" not in columns:
        op.add_column("task", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("task")}
    if "ix_task_archived_at" not in indexes:
        op.create_index("ix_task_archived_at", "task", ["archived_at"], unique=False)
    if "ix_task_deleted_at" not in indexes:
        op.create_index("ix_task_deleted_at", "task", ["deleted_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("task")}
    if "ix_task_deleted_at" in indexes:
        op.drop_index("ix_task_deleted_at", table_name="task")
    if "ix_task_archived_at" in indexes:
        op.drop_index("ix_task_archived_at", table_name="task")
    columns = {col["name"] for col in sa.inspect(bind).get_columns("task")}
    if "deleted_at" in columns:
        op.drop_column("task", "deleted_at")
    if "archived_at" in columns:
        op.drop_column("task", "archived_at")
