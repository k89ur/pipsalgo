"""create passkey-only signup challenges

Revision ID: 0012_passkey_signup_challenges
Revises: 0011_password_reset_tokens
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_passkey_signup_challenges"
down_revision = "0011_password_reset_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "passkey_signup_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("webauthn_user_id", sa.LargeBinary(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("challenge"),
        sa.UniqueConstraint("webauthn_user_id"),
    )
    op.create_index("ix_passkey_signup_challenges_expires_at", "passkey_signup_challenges", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_passkey_signup_challenges_expires_at", table_name="passkey_signup_challenges")
    op.drop_table("passkey_signup_challenges")
