"""Add local extracted-text status and searchable project document chunks."""
from alembic import op
import sqlalchemy as sa


revision = "d4f7a2b9c6e1"
down_revision = "c9e8f1a2b3d4"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("project_file")}
    additions = [
        ("extraction_status", sa.String(length=16), "pending"),
        ("extraction_error", sa.Text(), None),
        ("extracted_chars", sa.Integer(), 0),
        ("indexed_at", sa.DateTime(), None),
    ]
    for name, typ, default in additions:
        if name not in columns:
            # Nullable keeps upgrades safe for legacy rows; the ORM supplies
            # defaults for all newly created files.
            op.add_column("project_file", sa.Column(name, typ, nullable=True))
            if default is not None:
                bind.execute(sa.text(f"UPDATE project_file SET {name} = :value WHERE {name} IS NULL"), {"value": default})
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("project_file")}
    if "ix_project_file_extraction_status" not in indexes:
        op.create_index("ix_project_file_extraction_status", "project_file", ["extraction_status"], unique=False)

    if not _table_exists(bind, "project_document_chunk"):
        op.create_table(
            "project_document_chunk",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("project_file_id", sa.String(length=32), nullable=False),
            sa.Column("project_id", sa.String(length=32), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("locator", sa.String(length=256), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["project_file_id"], ["project_file.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_project_document_chunk_project_file_id", "project_document_chunk", ["project_file_id"], unique=False)
        op.create_index("ix_project_document_chunk_project_id", "project_document_chunk", ["project_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "project_document_chunk"):
        op.drop_index("ix_project_document_chunk_project_id", table_name="project_document_chunk")
        op.drop_index("ix_project_document_chunk_project_file_id", table_name="project_document_chunk")
        op.drop_table("project_document_chunk")
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("project_file")}
    if "ix_project_file_extraction_status" in indexes:
        op.drop_index("ix_project_file_extraction_status", table_name="project_file")
    columns = {col["name"] for col in sa.inspect(bind).get_columns("project_file")}
    for name in ("indexed_at", "extracted_chars", "extraction_error", "extraction_status"):
        if name in columns:
            op.drop_column("project_file", name)
