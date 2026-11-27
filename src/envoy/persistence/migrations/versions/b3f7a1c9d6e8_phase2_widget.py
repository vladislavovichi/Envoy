from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f7a1c9d6e8"
down_revision: str | Sequence[str] | None = "a9d2e6f8c3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mandate_widgets",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("selected", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
    )


def downgrade() -> None:
    op.drop_table("mandate_widgets")
