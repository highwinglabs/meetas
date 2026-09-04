"""Add recoverable trash metadata to project files."""
from alembic import op
import sqlalchemy as sa


revision = "e5a7c9d1f3b2"
down_revision = "d4f7a2b9c6e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("project_file")}
    if "deleted_at" not in columns:
        op.add_column("project_file", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("project_file")}
    if "ix_project_file_deleted_at" not in indexes:
        op.create_index("ix_project_file_deleted_at", "project_file", ["deleted_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("project_file")}
    if "ix_project_file_deleted_at" in indexes:
        op.drop_index("ix_project_file_deleted_at", table_name="project_file")
    columns = {col["name"] for col in sa.inspect(bind).get_columns("project_file")}
    if "deleted_at" in columns:
        op.drop_column("project_file", "deleted_at")
