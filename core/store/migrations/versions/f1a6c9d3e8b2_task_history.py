"""Add immutable task change history."""
from alembic import op
import sqlalchemy as sa


revision = "f1a6c9d3e8b2"
down_revision = "e8f4b2c6d1a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_history",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("meeting_id", sa.String(length=32), nullable=True),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_history_task_id", "task_history", ["task_id"], unique=False)
    op.create_index("ix_task_history_meeting_id", "task_history", ["meeting_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_task_history_meeting_id", table_name="task_history")
    op.drop_index("ix_task_history_task_id", table_name="task_history")
    op.drop_table("task_history")
