"""Add projects, project files and per-meeting settings.

All changes are additive. Existing meetings, recordings and transcripts remain
untouched; project assignment is optional and settings default to an empty
object for legacy rows.
"""
from alembic import op
import sqlalchemy as sa


revision = "c4e8f2a1d6b9"
down_revision = "b7c9d2e4f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Both additions are natively supported by SQLite and preserve all rows.
    # Project assignment is validated by the service layer; avoiding an
    # ALTER-constraint keeps this migration portable across SQLite versions.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    meeting_columns = {c["name"] for c in inspector.get_columns("meeting")}
    if "project_id" not in meeting_columns:
        op.add_column("meeting", sa.Column("project_id", sa.String(length=32), nullable=True))
    if "settings_json" not in meeting_columns:
        op.add_column(
            "meeting",
            sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        )
    if "ix_meeting_project_id" not in {i["name"] for i in inspector.get_indexes("meeting")}: 
        op.create_index("ix_meeting_project_id", "meeting", ["project_id"], unique=False)
    op.create_table(
        "project_file",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("meeting_id", sa.String(length=32), nullable=True),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="document"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_file_project_id", "project_file", ["project_id"], unique=False)
    op.create_index("ix_project_file_meeting_id", "project_file", ["meeting_id"], unique=False)


def downgrade() -> None:
    # Downgrade is intentionally explicit and only removes this migration's
    # own objects. Production code never calls downgrade automatically.
    op.drop_index("ix_project_file_meeting_id", table_name="project_file")
    op.drop_index("ix_project_file_project_id", table_name="project_file")
    op.drop_table("project_file")
    op.drop_index("ix_meeting_project_id", table_name="meeting")
    op.drop_column("meeting", "settings_json")
    op.drop_column("meeting", "project_id")
    op.drop_table("project")
