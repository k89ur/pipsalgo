"""create single-use email confirmations for TOTP disable

Revision ID: 0014
Revises: 0013_totp_setup_expiry
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_totp_disable_challenges"
down_revision = "0013_totp_setup_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "totp_disable_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("email_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash", name="uq_totp_disable_challenges_token_hash"),
    )
    op.create_index(
        "ix_totp_disable_challenges_user_id",
        "totp_disable_challenges",
        ["user_id"],
    )
    op.create_index(
        "ix_totp_disable_challenges_token_hash",
        "totp_disable_challenges",
        ["token_hash"],
    )
    op.create_index(
        "ix_totp_disable_challenges_email_hash",
        "totp_disable_challenges",
        ["email_hash"],
    )
    op.create_index(
        "ix_totp_disable_challenges_expires_at",
        "totp_disable_challenges",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_totp_disable_challenges_expires_at", table_name="totp_disable_challenges")
    op.drop_index("ix_totp_disable_challenges_email_hash", table_name="totp_disable_challenges")
    op.drop_index("ix_totp_disable_challenges_token_hash", table_name="totp_disable_challenges")
    op.drop_index("ix_totp_disable_challenges_user_id", table_name="totp_disable_challenges")
    op.drop_table("totp_disable_challenges")
