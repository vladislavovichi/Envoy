from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c71e0b9d42"
down_revision: str | Sequence[str] | None = "5f409c4aeca5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mandates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "version"),
    )
    op.create_index(op.f("ix_mandates_owner_id"), "mandates", ["owner_id"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.String(length=128), nullable=False),
        sa.Column("counterpart_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agreed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_owner_id"), "sessions", ["owner_id"], unique=False)
    op.create_index(
        "ix_sessions_chat", "sessions", ["connection_id", "counterpart_chat_id"], unique=False
    )
    op.create_table(
        "business_connections",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("rights", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "business_chats",
        sa.Column("connection_id", sa.String(length=128), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("last_incoming_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connection_id", "chat_id"),
    )
    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("update_id"),
    )
    op.create_table(
        "scheduled_jobs",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_scheduled_jobs_run_at"), "scheduled_jobs", ["run_at"], unique=False)
    op.create_index(
        op.f("ix_scheduled_jobs_session_id"), "scheduled_jobs", ["session_id"], unique=False
    )
    op.create_table(
        "llm_usage",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_index(op.f("ix_scheduled_jobs_session_id"), table_name="scheduled_jobs")
    op.drop_index(op.f("ix_scheduled_jobs_run_at"), table_name="scheduled_jobs")
    op.drop_table("scheduled_jobs")
    op.drop_table("processed_updates")
    op.drop_table("business_chats")
    op.drop_table("business_connections")
    op.drop_index("ix_sessions_chat", table_name="sessions")
    op.drop_index(op.f("ix_sessions_owner_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_mandates_owner_id"), table_name="mandates")
    op.drop_table("mandates")
