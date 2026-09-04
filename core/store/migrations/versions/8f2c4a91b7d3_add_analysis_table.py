"""add analysis table

Revision ID: 8f2c4a91b7d3
Revises: 1369a9a766ec
Create Date: 2026-08-30 15:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '8f2c4a91b7d3'
down_revision = '1369a9a766ec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('analysis',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_analysis_meeting_id'), 'analysis', ['meeting_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_analysis_meeting_id'), table_name='analysis')
    op.drop_table('analysis')
