from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d2e6f8c3b5"
down_revision: str | Sequence[str] | None = "f7c3d9b4e2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("counterpart_user_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(op.f("ix_invitations_session_id"), "invitations", ["session_id"], unique=False)
    op.create_table(
        "session_routes",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("route", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "agent_authorizations",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "bot_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_authorizations")
    op.drop_table("session_routes")
    op.drop_index(op.f("ix_invitations_session_id"), table_name="invitations")
    op.drop_table("invitations")
