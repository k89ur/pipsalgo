from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]

try:
    import qrcode
    from qrcode.image.svg import SvgImage
except ImportError:  # pragma: no cover
    qrcode = None  # type: ignore[assignment]
    SvgImage = None  # type: ignore[assignment]

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import TotpCredential, TotpLoginChallenge, User


TOTP_PERIOD_SECONDS = 30
TOTP_DIGITS = 6
TOTP_SECRET_BYTES = 20
TOTP_SETUP_TTL_SECONDS = 10 * 60
TOTP_LOGIN_TTL_SECONDS = 5 * 60
TOTP_MAX_LOGIN_ATTEMPTS = 5

_DEFAULT_KEY_FILE = Path(__file__).resolve().parents[2] / ".pipsgox" / "totp_encryption.key"


def _require_db():
    if SessionLocal is None:
        raise RuntimeError("PostgreSQL is not configured. Set DATABASE_URL first.")
    return SessionLocal


def _encryption_key() -> str:
    configured = os.getenv("PIPSGOX_TOTP_ENCRYPTION_KEY", "").strip()
    if configured:
        return configured

    path = Path(
        os.getenv("PIPSGOX_TOTP_ENCRYPTION_KEY_FILE", str(_DEFAULT_KEY_FILE))
    ).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        key = path.read_text(encoding="ascii").strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError("Could not read the TOTP encryption key file.") from exc

    if Fernet is None:
        raise RuntimeError("TOTP secret encryption requires the cryptography package.")

    key = Fernet.generate_key().decode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.encode("ascii"))
        finally:
            os.close(fd)
    except FileExistsError:
        try:
            key = path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RuntimeError("Could not read the TOTP encryption key file.") from exc
    except OSError as exc:
        raise RuntimeError("Could not create the TOTP encryption key file.") from exc

    if not key:
        raise RuntimeError("TOTP encryption key file is empty.")
    return key


def _cipher():
    if Fernet is None:
        raise RuntimeError("TOTP secret encryption requires the cryptography package.")
    try:
        return Fernet(_encryption_key().encode("ascii"))
    except Exception as exc:
        raise RuntimeError("TOTP encryption key is invalid.") from exc


def _encrypt_secret(secret: str) -> str:
    return _cipher().encrypt(secret.encode("ascii")).decode("ascii")


def _decrypt_secret(blob: str) -> str:
    try:
        return _cipher().decrypt(blob.encode("ascii")).decode("ascii")
    except Exception as exc:
        raise RuntimeError("Could not decrypt the TOTP authenticator secret.") from exc


def _normalize_secret(secret: str) -> str:
    return "".join(secret.strip().upper().split()).rstrip("=")


def _generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    normalized = _normalize_secret(secret)
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    digest = hmac.new(
        key,
        int(counter).to_bytes(8, "big", signed=False),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(binary % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_code(secret: str, for_time: float | None = None) -> str:
    timestamp = time.time() if for_time is None else float(for_time)
    counter = int(timestamp) // TOTP_PERIOD_SECONDS
    return _hotp(secret, counter)


def verify_user_code(user_id: int, code: str) -> bool:
    """Verify the current TOTP code for an enabled user account."""
    with _require_db()() as db:
        credential = _secret_exists(db, user_id)
        if credential is None or not credential.enabled:
            return False
        secret = _decrypt_secret(str(credential.secret_encrypted))
        return verify_code(secret, code)


def verify_code(
    secret: str,
    code: str,
    *,
    for_time: float | None = None,
    window: int = 1,
) -> bool:
    normalized_code = "".join(str(code).strip().split())
    if len(normalized_code) != TOTP_DIGITS or not normalized_code.isdigit():
        return False

    timestamp = time.time() if for_time is None else float(for_time)
    counter = int(timestamp) // TOTP_PERIOD_SECONDS
    for offset in range(-abs(window), abs(window) + 1):
        expected = _hotp(secret, counter + offset)
        if hmac.compare_digest(expected, normalized_code):
            return True
    return False


def _challenge_hash(challenge: str) -> str:
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


def _secret_exists(db: Session, user_id: int) -> TotpCredential | None:
    return db.scalar(
        select(TotpCredential).where(TotpCredential.user_id == int(user_id))
    )


def status(user_id: int) -> dict[str, object]:
    with _require_db()() as db:
        credential = _secret_exists(db, user_id)
        return {
            "enabled": bool(credential and credential.enabled),
            "configured": credential is not None,
        }


def setup(user_id: int) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    secret = _generate_secret()

    with _require_db()() as db:
        user = db.get(User, int(user_id))
        if user is None:
            raise ValueError("User does not exist.")

        existing = _secret_exists(db, user_id)
        if existing is not None and existing.enabled:
            raise ValueError("Authenticator app is already enabled.")

        if existing is None:
            credential = TotpCredential(
                user_id=int(user_id),
                secret_encrypted=_encrypt_secret(secret),
                enabled=False,
                confirmed_at=None,
            )
            db.add(credential)
        else:
            existing.secret_encrypted = _encrypt_secret(secret)
            existing.enabled = False
            existing.confirmed_at = None
            existing.last_used_at = None
        db.commit()

        username = str(user.username or user.email or "account")

    label = f"PIPSGOX:{username}"
    otpauth_uri = (
        "otpauth://totp/"
        + quote(label, safe="")
        + "?"
        + "secret="
        + quote(secret, safe="")
        + "&issuer=PIPSGOX&algorithm=SHA1&digits=6&period=30"
    )

    qr_data_url = None
    if qrcode is not None and SvgImage is not None:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=5,
            border=2,
            image_factory=SvgImage,
        )
        qr.add_data(otpauth_uri)
        qr.make(fit=True)
        svg = qr.make_image().to_string(encoding="unicode")
        encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        qr_data_url = "data:image/svg+xml;base64," + encoded

    return {
        "secret": secret,
        "otpauth_uri": otpauth_uri,
        "qr_code": qr_data_url,
        "expires_at": int((now + timedelta(seconds=TOTP_SETUP_TTL_SECONDS)).timestamp()),
    }


def confirm_setup(user_id: int, code: str) -> bool:
    with _require_db()() as db:
        credential = _secret_exists(db, user_id)
        if credential is None:
            raise ValueError("Start authenticator setup first.")

        secret = _decrypt_secret(str(credential.secret_encrypted))
        if not verify_code(secret, code):
            raise ValueError("Invalid authenticator code.")

        credential.enabled = True
        credential.confirmed_at = datetime.now(timezone.utc)
        credential.last_used_at = datetime.now(timezone.utc)
        db.commit()

    return True


def disable(user_id: int, code: str) -> bool:
    with _require_db()() as db:
        user = db.scalar(
            select(User).where(User.id == int(user_id)).with_for_update()
        )
        if user is None:
            raise ValueError("User account does not exist.")

        credential = _secret_exists(db, user_id)
        if credential is None or not credential.enabled:
            raise ValueError("Authenticator app is not enabled.")

        secret = _decrypt_secret(str(credential.secret_encrypted))
        if not verify_code(secret, code):
            raise ValueError("Invalid authenticator code.")

        # TOTP is a second factor, not a standalone primary sign-in method.
        # Fail closed if a future authentication change ever creates a
        # TOTP-only account.
        require_another_primary_method(db, int(user_id), "__totp__")

        db.delete(credential)
        db.commit()

    return True


def create_login_challenge(user_id: int) -> tuple[str, int]:
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TOTP_LOGIN_TTL_SECONDS)

    with _require_db()() as db:
        # Remove only expired challenges here. Do not invalidate another
        # still-valid challenge for the same user: a second browser tab or a
        # repeated sign-in must not silently break an earlier verification flow.
        db.execute(
            delete(TotpLoginChallenge).where(
                TotpLoginChallenge.expires_at <= now
            )
        )
        db.add(
            TotpLoginChallenge(
                challenge_hash=_challenge_hash(raw),
                user_id=int(user_id),
                attempts=0,
                expires_at=expires_at,
            )
        )
        db.commit()

    return raw, int(expires_at.timestamp())


def verify_login_challenge(challenge: str, code: str) -> tuple[int, dict[str, object]]:
    if not challenge or len(challenge) > 128:
        raise ValueError("Invalid authentication challenge.")

    now = datetime.now(timezone.utc)
    with _require_db()() as db:
        row = db.execute(
            select(TotpLoginChallenge, User.username, User.email, User.display_name)
            .join(User, User.id == TotpLoginChallenge.user_id)
            .where(TotpLoginChallenge.challenge_hash == _challenge_hash(challenge))
            .with_for_update()
        ).first()

        if row is None:
            raise ValueError("Authentication challenge expired. Sign in again.")

        login_challenge = row[0]
        if login_challenge.expires_at <= now:
            db.delete(login_challenge)
            db.commit()
            raise ValueError("Authentication challenge expired. Sign in again.")

        if login_challenge.attempts >= TOTP_MAX_LOGIN_ATTEMPTS:
            db.delete(login_challenge)
            db.commit()
            raise ValueError("Too many authenticator attempts. Sign in again.")

        credential = _secret_exists(db, int(login_challenge.user_id))
        if credential is None or not credential.enabled:
            db.delete(login_challenge)
            db.commit()
            raise ValueError("Authenticator setup is no longer enabled.")

        secret = _decrypt_secret(str(credential.secret_encrypted))
        if not verify_code(secret, code):
            login_challenge.attempts = int(login_challenge.attempts) + 1
            if login_challenge.attempts >= TOTP_MAX_LOGIN_ATTEMPTS:
                db.delete(login_challenge)
            db.commit()
            raise ValueError("Invalid authenticator code.")

        credential.last_used_at = now
        db.delete(login_challenge)
        db.commit()

        user = {
            "id": int(login_challenge.user_id),
            "username": str(row.username or ""),
            "email": row.email,
            "display_name": row.display_name,
        }
        return int(login_challenge.user_id), user


def cleanup_expired_challenges() -> int:
    with _require_db()() as db:
        result = db.execute(
            delete(TotpLoginChallenge).where(
                TotpLoginChallenge.expires_at <= datetime.now(timezone.utc)
            )
        )
        db.commit()
        return int(result.rowcount or 0)
