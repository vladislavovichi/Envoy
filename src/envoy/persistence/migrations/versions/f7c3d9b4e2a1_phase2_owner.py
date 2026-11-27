from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c3d9b4e2a1"
down_revision: str | Sequence[str] | None = "e5b8f1a2c7d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mandates",
        sa.Column("attestation", sa.String(length=16), server_default="platform", nullable=False),
    )
    op.create_table(
        "owner_keys",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.create_table(
        "signing_challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("bytes_to_sign", sa.LargeBinary(), nullable=False),
        sa.Column("context", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=True),
        sa.Column("result", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_signing_challenges_owner_id"), "signing_challenges", ["owner_id"], unique=False
    )
    op.create_table(
        "owner_delegations",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("agent_key", sa.LargeBinary(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "position"),
    )


def downgrade() -> None:
    op.drop_table("owner_delegations")
    op.drop_index(op.f("ix_signing_challenges_owner_id"), table_name="signing_challenges")
    op.drop_table("signing_challenges")
    op.drop_table("owner_keys")
    op.drop_column("mandates", "attestation")
