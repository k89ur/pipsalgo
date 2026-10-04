from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import delete, select, update

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.db.database import SessionLocal
from app.db.models import Passkey, PasskeyChallenge, PasskeySignupChallenge, User


CHALLENGE_TTL_SECONDS = 5 * 60
WEBAUTHN_TIMEOUT_MS = 60_000


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _web_origin() -> str:
    configured = _env("WEBAUTHN_ORIGIN")
    if configured:
        return configured.rstrip("/")
    configured = _env("PIPSGOX_WEB_URL")
    if configured:
        return configured.rstrip("/")
    codespace = _env("CODESPACE_NAME")
    domain = _env("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN") or "app.github.dev"
    if codespace:
        return f"https://{codespace}-3001.{domain}"
    # Keep the local WebAuthn origin on localhost. Do not use a loopback IP\n    # as the RP ID because browsers may reject IP-based RP IDs.\n    return "http://localhost:3001"


def _normalize_rp_id(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        candidate = urlparse(candidate).hostname or ""
    else:
        candidate = candidate.split("/", 1)[0]
        if candidate.startswith("[") and "]" in candidate:
            candidate = candidate[1:candidate.index("]")]
        elif candidate.count(":") == 1:
            candidate = candidate.rsplit(":", 1)[0]
    candidate = candidate.strip().rstrip(".")
    if not candidate:
        raise RuntimeError("WebAuthn RP ID is empty or invalid.")
    return candidate


def rp_id(origin: str | None = None) -> str:
    # For local development and reverse proxies, derive the RP ID from the
    # browser's actual origin. This prevents a backend-side localhost/port
    # assumption from producing a browser-invalid RP ID.
    if origin:
        hostname = urlparse(origin).hostname
        if hostname:
            return hostname

    configured = _env("WEBAUTHN_RP_ID")
    if configured:
        return _normalize_rp_id(configured)

    hostname = urlparse(_web_origin()).hostname
    if not hostname:
        raise RuntimeError("Could not determine the WebAuthn RP ID.")
    return hostname


def rp_name() -> str:
    return _env("WEBAUTHN_RP_NAME") or "PIPSGOX"


def _require_db():
    if SessionLocal is None:
        raise RuntimeError("PostgreSQL is not configured. Set DATABASE_URL first.")
    return SessionLocal


def _cleanup_expired_challenges(db) -> None:
    db.execute(
        delete(PasskeyChallenge).where(
            PasskeyChallenge.expires_at <= datetime.now(timezone.utc)
        )
    )


def _new_user_handle() -> bytes:
    return secrets.token_bytes(32)


def _ensure_user_handle(db, user: User) -> bytes:
    if user.webauthn_user_id:
        return bytes(user.webauthn_user_id)
    handle = _new_user_handle()
    user.webauthn_user_id = handle
    db.flush()
    return handle


def _store_challenge(
    db,
    *,
    challenge: bytes,
    ceremony: str,
    user_id: int | None,
) -> None:
    now = datetime.now(timezone.utc)
    db.add(
        PasskeyChallenge(
            challenge=challenge,
            user_id=user_id,
            ceremony=ceremony,
            created_at=now,
            expires_at=now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        )
    )


def _consume_challenge(
    *,
    challenge: bytes,
    ceremony: str,
    user_id: int | None = None,
) -> bool:
    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        _cleanup_expired_challenges(db)
        row = db.scalar(
            select(PasskeyChallenge)
            .where(
                PasskeyChallenge.challenge == challenge,
                PasskeyChallenge.ceremony == ceremony,
                PasskeyChallenge.used_at.is_(None),
                PasskeyChallenge.expires_at > datetime.now(timezone.utc),
                PasskeyChallenge.user_id == user_id
                if user_id is not None
                else PasskeyChallenge.user_id.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            db.commit()
            return False
        row.used_at = datetime.now(timezone.utc)
        db.commit()
        return True


def signup_registration_options(username: str, origin: str | None = None) -> dict[str, object]:
    """Create a short-lived WebAuthn registration ceremony without an existing account."""
    username = username.strip()
    if len(username) < 3 or len(username) > 64:
        raise ValueError("Username must be between 3 and 64 characters.")
    if any(char.isspace() for char in username):
        raise ValueError("Username cannot contain spaces.")

    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        _cleanup_expired_challenges(db)
        existing = db.scalar(select(User.id).where(User.username == username))
        if existing is not None:
            raise ValueError("Username already exists.")

        # A pending reservation prevents two simultaneous passkey signups
        # from racing for the same username.
        pending = db.scalar(
            select(PasskeySignupChallenge.id).where(
                PasskeySignupChallenge.username == username,
                PasskeySignupChallenge.used_at.is_(None),
                PasskeySignupChallenge.expires_at > datetime.now(timezone.utc),
            )
        )
        if pending is not None:
            raise ValueError("A passkey signup is already in progress for this username.")

        user_handle = _new_user_handle()
        options = generate_registration_options(
            rp_id=rp_id(origin),
            rp_name=rp_name(),
            user_id=user_handle,
            user_name=username,
            user_display_name=username,
            timeout=WEBAUTHN_TIMEOUT_MS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        now = datetime.now(timezone.utc)
        db.add(
            PasskeySignupChallenge(
                challenge=bytes(options.challenge),
                username=username,
                webauthn_user_id=user_handle,
                created_at=now,
                expires_at=now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
            )
        )
        db.commit()
        return json.loads(options_to_json(options))


def verify_passkey_signup(credential: dict[str, object], origin: str | None = None) -> tuple[int, dict[str, object]]:
    """Verify a passkey-only signup and atomically create the account."""
    if not isinstance(credential, dict):
        raise ValueError("Invalid passkey credential.")

    try:
        raw_id = base64url_to_bytes(str(credential["rawId"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid passkey credential ID.") from exc

    challenge = _challenge_from_client_data(credential, "webauthn.create")
    SessionLocalFactory = _require_db()

    # Verify the credential cryptographically before consuming the challenge.
    with SessionLocalFactory() as db:
        pending = db.scalar(
            select(PasskeySignupChallenge)
            .where(
                PasskeySignupChallenge.challenge == challenge,
                PasskeySignupChallenge.used_at.is_(None),
                PasskeySignupChallenge.expires_at > datetime.now(timezone.utc),
            )
        )
        if pending is None:
            raise ValueError("Passkey signup request expired or was already used.")
        expected_user_id = bytes(pending.webauthn_user_id)

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=origin or _web_origin(),
            expected_rp_id=rp_id(origin),
            require_user_verification=True,
        )
    except Exception as exc:
        raise ValueError("Passkey registration could not be verified.") from exc

    if not verification.user_verified:
        raise ValueError("User verification is required for passkey registration.")
    if verification.credential_id != raw_id:
        raise ValueError("Passkey credential ID mismatch.")

    with SessionLocalFactory() as db:
        pending = db.scalar(
            select(PasskeySignupChallenge)
            .where(
                PasskeySignupChallenge.challenge == challenge,
                PasskeySignupChallenge.used_at.is_(None),
                PasskeySignupChallenge.expires_at > datetime.now(timezone.utc),
            )
            .with_for_update()
        )
        if pending is None:
            raise ValueError("Passkey signup request expired or was already used.")

        if bytes(pending.webauthn_user_id) != expected_user_id:
            raise ValueError("Passkey signup user handle mismatch.")

        username = str(pending.username)
        if db.scalar(select(User.id).where(User.username == username)) is not None:
            raise ValueError("Username already exists.")

        if db.scalar(select(Passkey.id).where(Passkey.credential_id == verification.credential_id)) is not None:
            raise ValueError("This passkey is already registered.")

        user = User(
            username=username,
            webauthn_user_id=expected_user_id,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()

        db.add(
            Passkey(
                user_id=int(user.id),
                credential_id=verification.credential_id,
                public_key=verification.credential_public_key,
                sign_count=verification.sign_count,
                device_name=_device_name(credential),
            )
        )
        pending.used_at = datetime.now(timezone.utc)
        db.commit()

        return int(user.id), {
            "id": int(user.id),
            "username": username,
            "email": user.email,
            "display_name": user.display_name,
        }


def registration_options(user_id: int, origin: str | None = None) -> dict[str, object]:
    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        _cleanup_expired_challenges(db)
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("User does not exist.")
        if user.status != "active":
            raise ValueError("User account is not active.")

        user_handle = _ensure_user_handle(db, user)
        username = (user.username or user.email or f"user-{user.id}").strip()
        display_name = (user.display_name or username).strip()

        existing = db.scalars(
            select(Passkey.credential_id).where(Passkey.user_id == user_id)
        ).all()
        exclude_credentials = [
            PublicKeyCredentialDescriptor(id=bytes(credential_id))
            for credential_id in existing
        ]

        options = generate_registration_options(
            rp_id=rp_id(origin),
            rp_name=rp_name(),
            user_id=user_handle,
            user_name=username,
            user_display_name=display_name,
            timeout=WEBAUTHN_TIMEOUT_MS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude_credentials,
        )
        _store_challenge(
            db,
            challenge=bytes(options.challenge),
            ceremony="registration",
            user_id=user_id,
        )
        db.commit()

        return json.loads(options_to_json(options))


def verify_registration(user_id: int, credential: dict[str, object], origin: str | None = None) -> dict[str, object]:
    if not isinstance(credential, dict):
        raise ValueError("Invalid passkey credential.")

    try:
        raw_id = base64url_to_bytes(str(credential["rawId"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid passkey credential ID.") from exc

    challenge = _challenge_from_client_data(credential, "webauthn.create")

    if not _consume_challenge(
        challenge=challenge,
        ceremony="registration",
        user_id=user_id,
    ):
        raise ValueError("Passkey registration request expired or was already used.")

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=origin or _web_origin(),
            expected_rp_id=rp_id(origin),
            require_user_verification=True,
        )
    except Exception as exc:
        raise ValueError("Passkey registration could not be verified.") from exc

    if not verification.user_verified:
        raise ValueError("User verification is required for passkey registration.")

    if verification.credential_id != raw_id:
        raise ValueError("Passkey credential ID mismatch.")

    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("User does not exist.")

        duplicate = db.scalar(
            select(Passkey.id).where(Passkey.credential_id == verification.credential_id)
        )
        if duplicate is not None:
            raise ValueError("This passkey is already registered.")

        db.add(
            Passkey(
                user_id=user_id,
                credential_id=verification.credential_id,
                public_key=verification.credential_public_key,
                sign_count=verification.sign_count,
                device_name=_device_name(credential),
            )
        )
        db.commit()

    return {
        "registered": True,
        "credential_id": _bytes_to_base64url(verification.credential_id),
    }


def authentication_options(origin: str | None = None) -> dict[str, object]:
    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        _cleanup_expired_challenges(db)
        options = generate_authentication_options(
            rp_id=rp_id(origin),
            timeout=WEBAUTHN_TIMEOUT_MS,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        _store_challenge(
            db,
            challenge=bytes(options.challenge),
            ceremony="authentication",
            user_id=None,
        )
        db.commit()
        return json.loads(options_to_json(options))


def verify_authentication(credential: dict[str, object], origin: str | None = None) -> tuple[int, dict[str, object]]:
    if not isinstance(credential, dict):
        raise ValueError("Invalid passkey credential.")

    challenge = _challenge_from_client_data(credential, "webauthn.get")
    if not _consume_challenge(
        challenge=challenge,
        ceremony="authentication",
        user_id=None,
    ):
        raise ValueError("Passkey login request expired or was already used.")

    try:
        credential_id = base64url_to_bytes(str(credential["rawId"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid passkey credential ID.") from exc

    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        passkey = db.scalar(
            select(Passkey).where(Passkey.credential_id == credential_id)
        )
        if passkey is None:
            raise ValueError("Passkey is not registered.")

        user = db.get(User, passkey.user_id)
        if user is None or user.status != "active":
            raise ValueError("User account is not available.")

        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_origin=origin or _web_origin(),
                expected_rp_id=rp_id(origin),
                credential_public_key=bytes(passkey.public_key),
                credential_current_sign_count=int(passkey.sign_count),
                require_user_verification=True,
            )
        except Exception as exc:
            raise ValueError("Passkey authentication could not be verified.") from exc

        if not verification.user_verified:
            raise ValueError("User verification is required.")

        passkey.sign_count = verification.new_sign_count
        passkey.last_used_at = datetime.now(timezone.utc)
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()

        return int(user.id), {
            "id": int(user.id),
            "username": str(user.username or ""),
            "email": user.email,
            "display_name": user.display_name,
        }


def list_passkeys(user_id: int) -> list[dict[str, object]]:
    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        rows = db.scalars(
            select(Passkey)
            .where(Passkey.user_id == user_id)
            .order_by(Passkey.created_at.desc())
        ).all()
        return [
            {
                "id": int(row.id),
                "device_name": row.device_name,
                "created_at": row.created_at.isoformat(),
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            }
            for row in rows
        ]


def delete_passkey(user_id: int, passkey_id: int) -> bool:
    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        row = db.scalar(
            select(Passkey).where(
                Passkey.id == passkey_id,
                Passkey.user_id == user_id,
            )
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


def _challenge_from_client_data(
    credential: dict[str, object],
    expected_type: str,
) -> bytes:
    try:
        response = credential["response"]
        if not isinstance(response, dict):
            raise ValueError
        encoded = response["clientDataJSON"]
        raw = base64url_to_bytes(str(encoded))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("type") != expected_type:
            raise ValueError
        return base64url_to_bytes(str(payload["challenge"]))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid WebAuthn client data.") from exc


def _device_name(credential: dict[str, object]) -> str | None:
    response = credential.get("response")
    if not isinstance(response, dict):
        return None
    transports = response.get("transports")
    if isinstance(transports, list):
        labels = [str(item).strip() for item in transports if str(item).strip()]
        if labels:
            return ", ".join(labels)[:120]
    attachment = str(credential.get("authenticatorAttachment") or "").strip()
    return attachment[:120] or None


def _bytes_to_base64url(value: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
