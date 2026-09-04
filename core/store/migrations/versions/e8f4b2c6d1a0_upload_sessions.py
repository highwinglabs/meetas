"""Add resumable upload sessions."""
from alembic import op
import sqlalchemy as sa


revision = "e8f4b2c6d1a0"
down_revision = "d7f3a1c9e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upload_session",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("temp_path", sa.String(length=1024), nullable=False),
        sa.Column("total_size", sa.Integer(), nullable=False),
        sa.Column("received_size", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_session_project_id", "upload_session", ["project_id"], unique=False)
    op.create_index("ix_upload_session_status", "upload_session", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_upload_session_status", table_name="upload_session")
    op.drop_index("ix_upload_session_project_id", table_name="upload_session")
    op.drop_table("upload_session")
