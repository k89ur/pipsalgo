"""store TOTP setup expiry so the backend can enforce the setup TTL

Revision ID: 0013_totp_setup_expiry
Revises: 0012_passkey_signup_challenges
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_totp_setup_expiry"
down_revision = "0012_passkey_signup_challenges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "totp_credentials",
        sa.Column("setup_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_totp_credentials_setup_expires_at",
        "totp_credentials",
        ["setup_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_totp_credentials_setup_expires_at",
        table_name="totp_credentials",
    )
    op.drop_column("totp_credentials", "setup_expires_at")
