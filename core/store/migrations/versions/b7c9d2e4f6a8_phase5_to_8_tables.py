"""phase 5-8 tables: embeddings, tags, speaker labels, markers, revisions, tasks, backups

Revision ID: b7c9d2e4f6a8
Revises: 8f2c4a91b7d3
Create Date: 2026-08-30 18:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7c9d2e4f6a8'
down_revision = '8f2c4a91b7d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('segment_embedding',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('seg_id', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('dim', sa.Integer(), nullable=False),
    sa.Column('vector', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_segment_embedding_meeting_id'), 'segment_embedding', ['meeting_id'], unique=False)
    op.create_index('uq_segment_embedding', 'segment_embedding', ['meeting_id', 'seg_id', 'model'], unique=True)

    op.create_table('meeting_tag',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('tag', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meeting_tag_meeting_id'), 'meeting_tag', ['meeting_id'], unique=False)
    op.create_index('uq_meeting_tag', 'meeting_tag', ['meeting_id', 'tag'], unique=True)

    op.create_table('speaker_label',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('speaker_id', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=256), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_speaker_label_meeting_id'), 'speaker_label', ['meeting_id'], unique=False)
    op.create_index('uq_speaker_label', 'speaker_label', ['meeting_id', 'speaker_id'], unique=True)

    op.create_table('marker',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('at_s', sa.Float(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_marker_meeting_id'), 'marker', ['meeting_id'], unique=False)

    op.create_table('revision',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=False),
    sa.Column('segment_id', sa.String(length=32), nullable=False),
    sa.Column('field', sa.String(length=32), nullable=False),
    sa.Column('original', sa.Text(), nullable=False),
    sa.Column('updated', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_revision_meeting_id'), 'revision', ['meeting_id'], unique=False)

    op.create_table('task',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('meeting_id', sa.String(length=32), nullable=True),
    sa.Column('source_segment_id', sa.String(length=32), nullable=True),
    sa.Column('source_analysis_id', sa.String(length=32), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('responsible', sa.String(length=256), nullable=False),
    sa.Column('due_date', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('tags', sa.Text(), nullable=False),
    sa.Column('dedup_key', sa.String(length=64), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meeting.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_meeting_id'), 'task', ['meeting_id'], unique=False)
    op.create_index(op.f('ix_task_status'), 'task', ['status'], unique=False)
    op.create_index(op.f('ix_task_dedup_key'), 'task', ['dedup_key'], unique=False)

    op.create_table('backup_record',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('manifest_json', sa.Text(), nullable=False),
    sa.Column('note', sa.String(length=512), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('backup_record')
    op.drop_index(op.f('ix_task_dedup_key'), table_name='task')
    op.drop_index(op.f('ix_task_status'), table_name='task')
    op.drop_index(op.f('ix_task_meeting_id'), table_name='task')
    op.drop_table('task')
    op.drop_index(op.f('ix_revision_meeting_id'), table_name='revision')
    op.drop_table('revision')
    op.drop_index(op.f('ix_marker_meeting_id'), table_name='marker')
    op.drop_table('marker')
    op.drop_index('uq_speaker_label', table_name='speaker_label')
    op.drop_index(op.f('ix_speaker_label_meeting_id'), table_name='speaker_label')
    op.drop_table('speaker_label')
    op.drop_index('uq_meeting_tag', table_name='meeting_tag')
    op.drop_index(op.f('ix_meeting_tag_meeting_id'), table_name='meeting_tag')
    op.drop_table('meeting_tag')
    op.drop_index('uq_segment_embedding', table_name='segment_embedding')
    op.drop_index(op.f('ix_segment_embedding_meeting_id'), table_name='segment_embedding')
    op.drop_table('segment_embedding')
