from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c81e4b2f6a19"
down_revision: str | Sequence[str] | None = "a3c71e0b9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DASHBOARD_COLUMNS = (
    ("dashboard_chat_id", sa.BigInteger()),
    ("dashboard_message_id", sa.BigInteger()),
    ("dashboard_version", sa.Integer()),
    ("dashboard_revision", sa.Integer()),
)


def upgrade() -> None:
    for name, column_type in DASHBOARD_COLUMNS:
        op.add_column("sessions", sa.Column(name, column_type, nullable=True))
    op.create_table(
        "escalation_widgets",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("cause", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "cause"),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_escalation_widgets_message"),
    )
    op.create_table(
        "summary_widgets",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "day"),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_summary_widgets_message"),
    )
    op.create_table(
        "owner_summaries",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "day"),
    )
    op.create_table(
        "owner_summary_topics",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id"),
    )


def downgrade() -> None:
    op.drop_table("owner_summary_topics")
    op.drop_table("owner_summaries")
    op.drop_table("summary_widgets")
    op.drop_table("escalation_widgets")
    for name, _ in reversed(DASHBOARD_COLUMNS):
        op.drop_column("sessions", name)
