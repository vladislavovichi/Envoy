from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e9b2d4f1a7"
down_revision: str | Sequence[str] | None = "b3f7a1c9d6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "managed_bots",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("owner_user", sa.BigInteger(), nullable=False),
        sa.Column("secret", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("token", sa.LargeBinary(), nullable=True),
        sa.Column("trusted", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "request_id"),
        sa.UniqueConstraint("bot_id"),
    )


def downgrade() -> None:
    op.drop_table("managed_bots")
