from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.db.database import SessionLocal
from app.db.models import PasswordCredential, Session, User

logger = logging.getLogger("pipsgox.auth")

SESSION_COOKIE = "pipsgox_session"
SESSION_TTL_SECONDS = 60 * 60 * 12


def _require_db():
    if SessionLocal is None:
        raise RuntimeError("PostgreSQL is not configured. Set DATABASE_URL first.")
    return SessionLocal


def _password_hash(password: str, salt: bytes | None = None) -> str:
    if not password or len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "scrypt$16384$8$1$" + salt.hex() + "$" + digest.hex()


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _metadata_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize() -> None:
    _require_db()


def has_user() -> bool:
    with _require_db()() as db:
        return db.scalar(select(User.id).limit(1)) is not None


def verify_user_password(user_id: int, password: str) -> bool:
    if not password:
        return False
    with _require_db()() as db:
        password_hash = db.scalar(
            select(PasswordCredential.password_hash).where(
                PasswordCredential.user_id == user_id
            )
        )
    return password_hash is not None and _verify_password(password, str(password_hash))


def delete_all_users() -> int:
    """Delete all PostgreSQL users and their dependent credentials/sessions."""
    with _require_db()() as db:
        result = db.execute(delete(User))
        db.commit()
        return int(result.rowcount or 0)


def create_user(username: str, password: str) -> int:
    """Create a normal PIPSGOX user and return the new user id."""
    username = username.strip()
    if not username:
        raise ValueError("Username is required.")
    if len(username) < 3:
        raise ValueError("Username must be at least 3 characters.")
    if len(username) > 64:
        raise ValueError("Username must be at most 64 characters.")
    encoded = _password_hash(password)

    with _require_db()() as db:
        existing = db.scalar(select(User.id).where(User.username == username))
        if existing is not None:
            raise ValueError("Username already exists.")

        user = User(username=username)
        db.add(user)
        db.flush()

        db.add(
            PasswordCredential(
                user_id=user.id,
                password_hash=encoded,
            )
        )
        db.commit()
        return int(user.id)


def create_initial_user(username: str, password: str) -> None:
    """Backward-compatible wrapper for older scripts."""
    create_user(username, password)


def create_session_for_user(
    user_id: int,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Create a server-side session for an already authenticated user."""
    now = datetime.now(timezone.utc)
    raw_token = secrets.token_urlsafe(48)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)

    with _require_db()() as db:
        if db.get(User, user_id) is None:
            raise ValueError("User does not exist.")
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_login_at=now)
        )
        db.add(
            Session(
                user_id=user_id,
                session_token_hash=_token_hash(raw_token),
                expires_at=expires_at,
                last_seen_at=now,
                ip_hash=_metadata_hash(ip_address),
                user_agent=(user_agent or "")[:1024] or None,
            )
        )
        db.commit()

    return raw_token


def verify_credentials(username: str, password: str) -> int | None:
    """Verify a password without creating a session.

    This is used when an additional authentication factor must be completed
    before a full application session is issued.
    """
    username = username.strip()
    if not username or not password:
        return None

    SessionLocalFactory = _require_db()
    with SessionLocalFactory() as db:
        row = db.execute(
            select(User.id, PasswordCredential.password_hash)
            .join(
                PasswordCredential,
                PasswordCredential.user_id == User.id,
            )
            .where(User.username == username)
        ).first()

        if row is None:
            return None
        if not _verify_password(password, str(row.password_hash)):
            return None
        return int(row.id)


def authenticate(
    username: str,
    password: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    username = username.strip()
    if not username or not password:
        return None

    started = time.perf_counter()
    SessionLocalFactory = _require_db()
    db_opened = time.perf_counter()
    with SessionLocalFactory() as db:
        row = db.execute(
            select(User.id, User.username, PasswordCredential.password_hash)
            .join(
                PasswordCredential,
                PasswordCredential.user_id == User.id,
            )
            .where(User.username == username)
        ).first()
        query_done = time.perf_counter()

        if row is None:
            logger.info(
                "login timing username=%s result=unknown-user total=%.3fs db_open=%.3fs query=%.3fs",
                username,
                time.perf_counter() - started,
                db_opened - started,
                query_done - db_opened,
            )
            return None

        password_ok = _verify_password(password, str(row.password_hash))
        password_done = time.perf_counter()
        if not password_ok:
            logger.info(
                "login timing username=%s result=bad-password total=%.3fs db_open=%.3fs query=%.3fs password=%.3fs",
                username,
                time.perf_counter() - started,
                db_opened - started,
                query_done - db_opened,
                password_done - query_done,
            )
            return None

        user_id = int(row.id)
        now = datetime.now(timezone.utc)
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_login_at=now)
        )
        update_done = time.perf_counter()

        raw_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)

        db.add(
            Session(
                user_id=user_id,
                session_token_hash=_token_hash(raw_token),
                expires_at=expires_at,
                last_seen_at=now,
                ip_hash=_metadata_hash(ip_address),
                user_agent=(user_agent or "")[:1024] or None,
            )
        )
        db.commit()
        commit_done = time.perf_counter()

    logger.info(
        "login timing username=%s result=success total=%.3fs db_open=%.3fs query=%.3fs password=%.3fs update=%.3fs commit=%.3fs",
        username,
        commit_done - started,
        db_opened - started,
        query_done - db_opened,
        password_done - query_done,
        update_done - password_done,
        commit_done - update_done,
    )
    return raw_token


def get_user(token: str | None) -> dict[str, object] | None:
    if not token:
        return None

    now = datetime.now(timezone.utc)
    token_hash = _token_hash(token)

    with _require_db()() as db:
        row = db.execute(
            select(
                Session.user_id,
                Session.expires_at,
                User.username,
                User.email,
                User.display_name,
            )
            .join(User, User.id == Session.user_id)
            .where(
                Session.session_token_hash == token_hash,
                Session.expires_at > now,
                Session.revoked_at.is_(None),
            )
        ).first()

        if row is None:
            return None

        db.execute(
            update(Session)
            .where(Session.session_token_hash == token_hash)
            .values(last_seen_at=now)
        )
        db.commit()

    return {
        "id": int(row.user_id),
        "username": str(row.username or ""),
        "email": row.email,
        "display_name": row.display_name,
        "expires_at": int(row.expires_at.timestamp()),
    }


def list_sessions(user_id: int, current_token: str | None = None) -> list[dict[str, object]]:
    """Return safe metadata for the user's active sessions."""
    now = datetime.now(timezone.utc)
    current_hash = _token_hash(current_token) if current_token else None
    with _require_db()() as db:
        rows = db.execute(
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.expires_at > now,
                Session.revoked_at.is_(None),
            )
            .order_by(Session.last_seen_at.desc().nullslast(), Session.created_at.desc())
        ).scalars().all()

    return [
        {
            "id": int(session.id),
            "created_at": session.created_at.isoformat(),
            "last_seen_at": session.last_seen_at.isoformat() if session.last_seen_at else None,
            "expires_at": session.expires_at.isoformat(),
            "user_agent": session.user_agent,
            "current": bool(current_hash and hmac.compare_digest(session.session_token_hash, current_hash)),
        }
        for session in rows
    ]


def revoke_session(user_id: int, session_id: int) -> bool:
    """Revoke one active session belonging to the user."""
    now = datetime.now(timezone.utc)
    with _require_db()() as db:
        result = db.execute(
            update(Session)
            .where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        db.commit()
        return bool(result.rowcount)


def revoke_other_sessions(user_id: int, current_token: str | None) -> int:
    """Revoke every active session except the caller's current session."""
    now = datetime.now(timezone.utc)
    current_hash = _token_hash(current_token) if current_token else None
    with _require_db()() as db:
        statement = (
            update(Session)
            .where(
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
                Session.expires_at > now,
            )
            .values(revoked_at=now)
        )
        if current_hash:
            statement = statement.where(Session.session_token_hash != current_hash)
        result = db.execute(statement)
        db.commit()
        return int(result.rowcount or 0)


def revoke(token: str | None) -> None:
    if not token:
        return

    with _require_db()() as db:
        db.execute(
            delete(Session).where(
                Session.session_token_hash == _token_hash(token)
            )
        )
        db.commit()


def revoke_all_sessions(user_id: int | None = None) -> int:
    with _require_db()() as db:
        statement = delete(Session)
        if user_id is not None:
            statement = statement.where(Session.user_id == user_id)
        result = db.execute(statement)
        db.commit()
        return int(result.rowcount or 0)


def revoke_all(user_id: int) -> int:
    return revoke_all_sessions(user_id)


def cleanup_expired_sessions() -> int:
    """Remove expired sessions from PostgreSQL."""
    with _require_db()() as db:
        result = db.execute(
            delete(Session).where(Session.expires_at <= datetime.now(timezone.utc))
        )
        db.commit()
        return int(result.rowcount or 0)
