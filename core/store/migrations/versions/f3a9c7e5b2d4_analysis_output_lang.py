"""Store the analysis output language on the analysis row.

Revision ID: f3a9c7e5b2d4
Revises: e5a7c9d1f3b2
Create Date: 2026-09-05 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "f3a9c7e5b2d4"
down_revision = "e5a7c9d1f3b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("analysis")}
    if "output_lang" not in columns:
        op.add_column("analysis", sa.Column("output_lang", sa.String(length=8), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("analysis")}
    if "output_lang" in columns:
        op.drop_column("analysis", "output_lang")
