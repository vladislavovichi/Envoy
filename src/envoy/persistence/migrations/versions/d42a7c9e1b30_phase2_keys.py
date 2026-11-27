from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d42a7c9e1b30"
down_revision: str | Sequence[str] | None = "c81e4b2f6a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "peer_pins",
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("owner_key", sa.LargeBinary(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("bot_id"),
    )
    op.create_table(
        "peer_key_history",
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("position", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("agent_key", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("bot_id", "position"),
    )


def downgrade() -> None:
    op.drop_table("peer_key_history")
    op.drop_table("peer_pins")
