from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class PasskeySignupChallenge(Base):
    """Short-lived state for passkey-only account registration."""

    __tablename__ = "passkey_signup_challenges"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    webauthn_user_id: Mapped[bytes] = mapped_column(LargeBinary(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
