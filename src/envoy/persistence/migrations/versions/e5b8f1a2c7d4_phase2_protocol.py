from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b8f1a2c7d4"
down_revision: str | Sequence[str] | None = "d42a7c9e1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "protocol_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("peer_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("agreed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_protocol_sessions_owner_id"), "protocol_sessions", ["owner_id"], unique=False
    )
    op.create_index(
        op.f("ix_protocol_sessions_next_due"), "protocol_sessions", ["next_due"], unique=False
    )
    op.create_index(
        "ix_protocol_sessions_pair",
        "protocol_sessions",
        ["bot_id", "peer_bot_id", "closed"],
        unique=False,
    )
    op.create_table(
        "protocol_updates",
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("bot_id", "update_id"),
    )
    op.create_table(
        "protocol_nonces",
        sa.Column("sender", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sender", "nonce"),
    )
    op.create_index(
        op.f("ix_protocol_nonces_expires_at"), "protocol_nonces", ["expires_at"], unique=False
    )
    op.create_table(
        "protocol_evidence",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("text_ciphertext", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "bot_id", "update_id"),
    )


def downgrade() -> None:
    op.drop_table("protocol_evidence")
    op.drop_index(op.f("ix_protocol_nonces_expires_at"), table_name="protocol_nonces")
    op.drop_table("protocol_nonces")
    op.drop_table("protocol_updates")
    op.drop_index("ix_protocol_sessions_pair", table_name="protocol_sessions")
    op.drop_index(op.f("ix_protocol_sessions_next_due"), table_name="protocol_sessions")
    op.drop_index(op.f("ix_protocol_sessions_owner_id"), table_name="protocol_sessions")
    op.drop_table("protocol_sessions")
