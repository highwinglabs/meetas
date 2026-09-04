"""Keep immutable snapshots when a meeting is transcribed again."""
from alembic import op
import sqlalchemy as sa


revision = "d7f3a1c9e2b4"
down_revision = "c4e8f2a1d6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcript_version",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("meeting_id", sa.String(length=32), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("segments_json", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meeting.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcript_version_meeting_id", "transcript_version", ["meeting_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transcript_version_meeting_id", table_name="transcript_version")
    op.drop_table("transcript_version")
