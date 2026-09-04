"""Add lifecycle metadata for the workspace UX.

All changes are additive: existing meetings, projects and tasks remain intact.
"""
from alembic import op
import sqlalchemy as sa


revision = "a2c7e9f4b1d6"
down_revision = "f1a6c9d3e8b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    additions = {
        "meeting": [
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("transcript_coverage", sa.Float(), nullable=True),
            sa.Column("transcript_coverage_note", sa.Text(), nullable=True),
        ],
        "project": [sa.Column("deleted_at", sa.DateTime(), nullable=True)],
        "task": [sa.Column("project_id", sa.String(length=32), nullable=True)],
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    for name, table, columns in (
        ("ix_meeting_deleted_at", "meeting", ["deleted_at"]),
        ("ix_project_deleted_at", "project", ["deleted_at"]),
        ("ix_task_project_id", "task", ["project_id"]),
    ):
        if name not in {index["name"] for index in inspector.get_indexes(table)}:
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    op.drop_index("ix_task_project_id", table_name="task")
    op.drop_index("ix_project_deleted_at", table_name="project")
    op.drop_index("ix_meeting_deleted_at", table_name="meeting")
    op.drop_column("task", "project_id")
    op.drop_column("project", "deleted_at")
    op.drop_column("meeting", "transcript_coverage_note")
    op.drop_column("meeting", "transcript_coverage")
    op.drop_column("meeting", "archived_at")
    op.drop_column("meeting", "deleted_at")
