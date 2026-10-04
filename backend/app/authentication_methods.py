from __future__ import annotations

from sqlalchemy import select

from app.db.models import OAuthAccount, Passkey, PasswordCredential


def primary_authentication_methods(db, user_id: int) -> list[str]:
    """Return usable primary sign-in methods for an account."""
    methods: list[str] = []
    if db.scalar(select(PasswordCredential.id).where(PasswordCredential.user_id == int(user_id))) is not None:
        methods.append("password")
    if db.scalar(select(Passkey.id).where(Passkey.user_id == int(user_id))) is not None:
        methods.append("passkey")
    if db.scalar(select(OAuthAccount.id).where(OAuthAccount.user_id == int(user_id))) is not None:
        methods.append("oauth")
    return methods


def require_another_primary_method(db, user_id: int, method_being_removed: str) -> None:
    methods = primary_authentication_methods(db, user_id)
    if not [method for method in methods if method != method_being_removed]:
        raise ValueError(
            "This is your only sign-in method. Add another sign-in method first "
            "(another passkey, a password, or a supported OAuth sign-in) before removing it. "
            "Authenticator App (TOTP), recovery codes, and email verification are additional "
            "security/recovery methods and do not replace a primary sign-in method."
        )
