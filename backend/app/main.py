from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import logging
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import replace
from typing import Literal
from urllib.parse import urlencode, urlsplit

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from fyers_apiv3.FyersWebsocket import data_ws

from app.providers.fyers import FyersMarketDataProvider
from app import auth, broker_accounts, security_audit, diagnostics, oidc, passkey_service, totp_service, recovery_codes, email_verification, account_recovery
from app.broker_manager import BrokerManager
from app.ma44_scanner import scanner as ma44_scanner
from app.market_data_master import configured_source, market_data_master

load_dotenv()

app = FastAPI(title="PIPSGOX API", version="0.5.0")


def _ensure_background_scanners() -> None:
    # The scanner can perform hundreds of network requests when it starts.
    # Do not launch that workload before authentication; otherwise a fresh
    # login can compete with the scanner for CPU/network resources.
    ma44_scanner.start()
logger = logging.getLogger("pipsgox.market_data")

_trusted_hosts = [
    item.strip() for item in os.getenv(
        "PIPSGOX_TRUSTED_HOSTS",
        "localhost,127.0.0.1,*.app.github.dev",
    ).split(",") if item.strip()
]
if _trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)

if os.getenv("PIPSGOX_FORCE_HTTPS", "0").strip().lower() in {"1", "true", "yes"}:
    app.add_middleware(HTTPSRedirectMiddleware)

broker_accounts.initialize()
auth.initialize()
# PIPSGOX is currently a single-owner installation. Broker configuration must
# not survive after the owner has been removed.
if not auth.has_user():
    broker_accounts.reset_all()
diagnostics.initialize()

def _codespace_forwarded_url(port: int) -> str:
    codespace_name = os.getenv("CODESPACE_NAME", "").strip()
    forwarding_domain = os.getenv(
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN",
        "app.github.dev",
    ).strip()
    if codespace_name and forwarding_domain:
        return f"https://{codespace_name}-{port}.{forwarding_domain}"
    # Use localhost for local WebAuthn development. WebAuthn RP IDs are\n    # domain identifiers; localhost is the supported local-development name.\n    return f"http://localhost:{port}"


PIPSGOX_WEB_URL = os.getenv(
    "PIPSGOX_WEB_URL",
    _codespace_forwarded_url(3001),
).strip()
FYERS_REDIRECT_URI = os.getenv(
    "FYERS_REDIRECT_URI",
    f"{_codespace_forwarded_url(8000)}/auth/broker/fyers/callback",
).strip()


# Dhan OAuth state is persisted in broker_accounts.broker_oauth_states so the
# callback survives backend restarts during the external login flow.
_LOGIN_ATTEMPTS: dict[str, tuple[int, float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300
_PASSWORD_RESET_ATTEMPTS: dict[str, tuple[int, float]] = {}
_PASSWORD_RESET_MAX_ATTEMPTS = 5
_PASSWORD_RESET_WINDOW_SECONDS = 900
_PASSKEY_SIGNUP_ATTEMPTS: dict[str, tuple[int, float]] = {}
_PASSKEY_SIGNUP_MAX_ATTEMPTS = 8
_PASSKEY_SIGNUP_WINDOW_SECONDS = 900
_order_idempotency_lock = threading.Lock()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[PIPSGOX_WEB_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_COOKIE_SECURE = PIPSGOX_WEB_URL.startswith("https://")

def _request_user(request: Request) -> dict[str, object] | None:
    return auth.get_user(request.cookies.get(auth.SESSION_COOKIE))


def _diagnostic_account_id(request: Request) -> int | None:
    raw = request.query_params.get("account_id")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    parts = [part for part in request.url.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "accounts" and parts[index + 1].isdigit():
            return int(parts[index + 1])
    return None


def _configured_web_origin() -> str:
    parsed = urlsplit(PIPSGOX_WEB_URL)
    return f"{parsed.scheme}://{parsed.netloc}"


def _request_origin_matches_web_app(request: Request) -> bool:
    expected = _configured_web_origin()
    origin = request.headers.get("origin", "").strip()
    if origin:
        return origin.rstrip("/") == expected.rstrip("/")

    referer = request.headers.get("referer", "").strip()
    if referer:
        parsed = urlsplit(referer)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/") == expected.rstrip("/")

    return False


@app.middleware("http")
async def require_private_api(request: Request, call_next):
    path = request.url.path

    # State-changing API requests must prove that they originated from the
    # configured PIPSGOX web origin. SameSite=Lax remains defense-in-depth;
    # it is not the sole CSRF control.
    if (
        path.startswith("/api/")
        and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        and not _request_origin_matches_web_app(request)
    ):
        return Response(
            content='{"detail":"Cross-site request blocked."}',
            status_code=403,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "Vary": "Origin, Referer"},
        )

    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if _request_user(request) is None:
            return Response(
                content='{"detail":"Authentication required."}',
                status_code=401,
                media_type="application/json",
            )
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if _request_user(request) is None:
            return Response(
                content='{"detail":"Authentication required."}',
                status_code=401,
                media_type="application/json",
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if path.startswith("/api/") or path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
    if (
        path.startswith("/api/")
        and not path.startswith("/api/auth/")
        and not path.startswith("/api/diagnostics/")
        and 200 <= response.status_code < 400
    ):
        parsed_account_id = _diagnostic_account_id(request)
        diagnostics.resolve_service(service=path, account_id=parsed_account_id)

    if (
        path.startswith("/api/")
        and not path.startswith("/api/auth/")
        and not path.startswith("/api/diagnostics/")
        and response.status_code >= 400
    ):
        parsed_account_id = _diagnostic_account_id(request)
        detail = ""
        try:
            body = getattr(response, "body", b"")
            if body:
                payload = json.loads(body.decode("utf-8"))
                detail = str(payload.get("detail") or payload.get("message") or "") if isinstance(payload, dict) else ""
        except Exception:
            detail = ""
        diagnostics.record(
            severity="ERROR" if response.status_code >= 500 or response.status_code in {409, 422} else "WARNING",
            category="HTTP",
            component="API",
            service=path,
            error_code=f"PIP-API-HTTP-{response.status_code}",
            message=detail or f"{request.method} {path} returned HTTP {response.status_code}.",
            account_id=parsed_account_id,
            http_status=response.status_code,
            technical_detail=detail,
        )
    return response


class AuthCredentials(BaseModel):
    username: str
    password: str


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is not None:
        _ensure_background_scanners()
    return {
        "authenticated": user is not None,
        "username": user["username"] if user else None,
        "registration_enabled": True,
    }


class PasskeySignupPayload(BaseModel):
    username: str


@app.post("/api/auth/passkey/signup/options")
def passkey_signup_options(payload: PasskeySignupPayload, request: Request) -> dict[str, object]:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts, started = _PASSKEY_SIGNUP_ATTEMPTS.get(ip, (0, now))
    if now - started >= _PASSKEY_SIGNUP_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts >= _PASSKEY_SIGNUP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many passkey signup attempts. Try again later.")

    try:
        return passkey_service.signup_registration_options(
            payload.username,
            origin=request.headers.get("origin"),
        )
    except ValueError as exc:
        _PASSKEY_SIGNUP_ATTEMPTS[ip] = (attempts + 1, started)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class PasskeyCredentialPayload(BaseModel):
    credential: dict[str, object]


@app.post("/api/auth/passkey/signup/verify")
def passkey_signup_verify(
    payload: PasskeyCredentialPayload,
    request: Request,
    response: Response,
) -> dict[str, object]:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts, started = _PASSKEY_SIGNUP_ATTEMPTS.get(ip, (0, now))
    if now - started >= _PASSKEY_SIGNUP_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts >= _PASSKEY_SIGNUP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many passkey signup attempts. Try again later.")

    try:
        user_id, user = passkey_service.verify_passkey_signup(
            payload.credential,
            origin=request.headers.get("origin"),
        )
        token = auth.create_session_for_user(
            user_id,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        _PASSKEY_SIGNUP_ATTEMPTS[ip] = (attempts + 1, started)
        security_audit.record("passkey_signup", success=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _PASSKEY_SIGNUP_ATTEMPTS.pop(ip, None)
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
        path="/",
    )
    security_audit.record(
        "passkey_signup",
        username=str(user["username"]),
        success=True,
    )
    _ensure_background_scanners()
    return {"authenticated": True, **user}


@app.post("/api/auth/signup")
def auth_signup(payload: AuthCredentials, request: Request, response: Response) -> dict[str, object]:
    try:
        auth.create_user(payload.username, payload.password)
        token = auth.authenticate(
            payload.username,
            payload.password,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not token:
        raise HTTPException(status_code=500, detail="Account was created but the session could not be established.")

    response.set_cookie(
        auth.SESSION_COOKIE, token, httponly=True, secure=SESSION_COOKIE_SECURE,
        samesite="lax", max_age=auth.SESSION_TTL_SECONDS, path="/",
    )
    _ensure_background_scanners()
    security_audit.record("signup", username=payload.username.strip(), success=True)
    return {"authenticated": True, "username": payload.username.strip()}


@app.post("/api/auth/login")
def auth_login(payload: AuthCredentials, request: Request, response: Response) -> dict[str, object]:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    attempts, started = _LOGIN_ATTEMPTS.get(ip, (0, now))
    if now - started >= _LOGIN_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    user_id = auth.verify_credentials(payload.username, payload.password)
    if user_id is None:
        _LOGIN_ATTEMPTS[ip] = (attempts + 1, started)
        security_audit.record("login", username=payload.username.strip(), success=False)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    if totp_service.status(user_id)["enabled"]:
        challenge, expires_at = totp_service.create_login_challenge(user_id)
        security_audit.record(
            "login_password_verified_totp_required",
            username=payload.username.strip(),
            success=True,
        )
        return {
            "authenticated": False,
            "totp_required": True,
            "challenge_id": challenge,
            "challenge_expires_at": expires_at,
        }

    token = auth.create_session_for_user(
        user_id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    _LOGIN_ATTEMPTS.pop(ip, None)
    security_audit.record("login", username=payload.username.strip(), success=True)
    response.set_cookie(
        auth.SESSION_COOKIE, token, httponly=True, secure=SESSION_COOKIE_SECURE,
        samesite="lax", max_age=auth.SESSION_TTL_SECONDS, path="/",
    )
    _ensure_background_scanners()
    return {"authenticated": True, "username": payload.username.strip()}


class EmailPayload(BaseModel):
    email: str


class PasswordResetRequestPayload(BaseModel):
    email: str


class PasswordResetConfirmPayload(BaseModel):
    token: str
    password: str


class PasswordResetDevPayload(BaseModel):
    email: str


class TotpCodePayload(BaseModel):
    code: str


class TotpLoginPayload(BaseModel):
    challenge_id: str
    code: str


@app.post("/api/auth/login/totp")
def auth_login_totp(
    payload: TotpLoginPayload,
    request: Request,
    response: Response,
) -> dict[str, object]:
    return _complete_totp_protected_login(
        payload.challenge_id,
        payload.code,
        request,
        response,
        audit_event="totp_login",
        verifier="totp",
    )


@app.post("/api/auth/login/recovery")
def auth_login_recovery(
    payload: TotpLoginPayload,
    request: Request,
    response: Response,
) -> dict[str, object]:
    return _complete_totp_protected_login(
        payload.challenge_id,
        payload.code,
        request,
        response,
        audit_event="recovery_code_login",
        verifier="recovery",
    )


def _complete_totp_protected_login(
    challenge_id: str,
    code: str,
    request: Request,
    response: Response,
    *,
    audit_event: str,
    verifier: str,
) -> dict[str, object]:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    attempts, started = _LOGIN_ATTEMPTS.get(ip, (0, now))
    if now - started >= _LOGIN_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    try:
        if verifier == "totp":
            user_id, user = totp_service.verify_login_challenge(
                challenge_id,
                code,
            )
        else:
            # Recovery-code verification binds the code to the short-lived
            # password+TOTP challenge and consumes both atomically.
            user_id, user = recovery_codes.verify_login_code(
                challenge_id,
                code,
            )

        token = auth.create_session_for_user(
            user_id,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        _LOGIN_ATTEMPTS[ip] = (attempts + 1, started)
        security_audit.record(audit_event, success=False)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _LOGIN_ATTEMPTS.pop(ip, None)
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
        path="/",
    )
    security_audit.record(
        audit_event,
        username=str(user["username"]),
        success=True,
    )
    _ensure_background_scanners()
    return {"authenticated": True, **user}


@app.get("/api/auth/oauth/{provider}/start")
def oauth_start(provider: str, request: Request):
    provider = provider.lower()
    if provider not in {"google", "apple"}:
        raise HTTPException(status_code=404, detail="Unsupported OAuth provider.")
    try:
        url = oidc.build_authorization_url(
            provider,
            request.cookies.get(auth.SESSION_COOKIE),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/auth/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    response: Response,
    state: str = "",
    code: str = "",
    error: str | None = None,
):
    provider = provider.lower()
    if provider not in {"google", "apple"}:
        raise HTTPException(status_code=404, detail="Unsupported OAuth provider.")

    try:
        token = oidc.complete_callback(
            provider=provider,
            state=state,
            code=code,
            session_token=request.cookies.get(auth.SESSION_COOKIE),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            error=error,
        )
    except Exception:
        # Do not expose provider responses, tokens, claims, or validation
        # details to the browser. The frontend receives a generic error.
        return RedirectResponse(
            url=f"{PIPSGOX_WEB_URL}/?oauth_error=oauth_failed",
            status_code=303,
        )

    response = RedirectResponse(
        url=f"{PIPSGOX_WEB_URL}/?oauth=success",
        status_code=303,
    )
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
        path="/",
    )
    return response


@app.get("/api/security/sessions")
def security_sessions(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = request.cookies.get(auth.SESSION_COOKIE)
    try:
        sessions = auth.list_sessions(int(user["id"]), token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"sessions": sessions}


@app.delete("/api/security/sessions/{session_id}")
def security_revoke_session(session_id: int, request: Request, response: Response) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = request.cookies.get(auth.SESSION_COOKIE)
    try:
        sessions = auth.list_sessions(int(user["id"]), token)
        target = next((item for item in sessions if int(item["id"]) == session_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        revoked = auth.revoke_session(int(user["id"]), session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found.")
    security_audit.record(
        "session_revoke",
        username=str(user["username"]),
        success=True,
    )
    if bool(target["current"]):
        response.delete_cookie(
            auth.SESSION_COOKIE,
            path="/",
            secure=SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="lax",
        )
    return {"revoked": True, "current": bool(target["current"])}


@app.post("/api/security/sessions/logout-others")
def security_logout_other_sessions(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        count = auth.revoke_other_sessions(
            int(user["id"]),
            request.cookies.get(auth.SESSION_COOKIE),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    security_audit.record(
        "logout_other_sessions",
        username=str(user["username"]),
        success=True,
    )
    return {"revoked_sessions": count}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    # Logout must be a fast, single session-revocation operation. Do not call
    # _request_user()/auth.get_user() here because get_user() also writes
    # last_seen_at and commits a second database transaction before logout.
    auth.revoke(request.cookies.get(auth.SESSION_COOKIE))
    security_audit.record("logout", success=True)
    # Match the session cookie attributes used by login so browsers reliably
    # remove the cookie on every supported deployment.
    response.delete_cookie(
        auth.SESSION_COOKIE,
        path="/",
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return {"authenticated": False}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


@app.get("/api/auth/recovery-codes")
def recovery_codes_status(request: Request) -> dict[str, int]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return recovery_codes.status(int(user["id"]))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class RecoveryCodesGeneratePayload(BaseModel):
    code: str


@app.post("/api/auth/recovery-codes/generate")
def recovery_codes_generate(
    payload: RecoveryCodesGeneratePayload,
    request: Request,
) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        codes = recovery_codes.generate(
            int(user["id"]),
            payload.code,
            totp_service.verify_user_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    security_audit.record(
        "recovery_codes_generate",
        username=str(user["username"]),
        success=True,
    )
    return {"generated": True, "codes": codes, "count": len(codes)}


@app.post("/api/auth/password-reset/request")
def password_reset_request(payload: PasswordResetRequestPayload, request: Request) -> dict[str, str]:
    # Always return the same response for valid and unknown addresses.
    # Rate limiting is intentionally silent to avoid becoming an account oracle.
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    attempts, started = _PASSWORD_RESET_ATTEMPTS.get(ip, (0, now))
    if now - started >= _PASSWORD_RESET_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts < _PASSWORD_RESET_MAX_ATTEMPTS:
        _PASSWORD_RESET_ATTEMPTS[ip] = (attempts + 1, started)
        try:
            account_recovery.request_reset(payload.email)
        except ValueError:
            # Invalid input receives the same generic response as an unknown
            # account. Do not expose account existence through this endpoint.
            pass
        except Exception:
            logger.exception("Password reset delivery failed.")
    else:
        _PASSWORD_RESET_ATTEMPTS[ip] = (attempts, started)

    return {
        "message": "If an account exists for that email, password reset instructions will be sent."
    }


@app.post("/api/auth/password-reset/confirm")
def password_reset_confirm(payload: PasswordResetConfirmPayload) -> dict[str, object]:
    try:
        result = account_recovery.confirm_reset(payload.token, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    security_audit.record(
        "password_reset_completed",
        username=str(result.get("username") or ""),
        success=True,
    )
    return {"reset": True}


def _password_reset_dev_enabled(request: Request) -> bool:
    if os.getenv("PIPSGOX_DEV_PASSWORD_RESET", "0").strip().lower() not in {"1", "true", "yes"}:
        return False
    host = (request.url.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


@app.post("/api/auth/password-reset/dev-token")
def password_reset_dev_token(
    request: Request, payload: PasswordResetDevPayload
) -> dict[str, str]:
    if not _password_reset_dev_enabled(request):
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        token = account_recovery.create_dev_token(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"token": token}


@app.get("/api/auth/email")
def email_status(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return email_verification.status(int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/auth/email")
def email_set(request: Request, payload: EmailPayload) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        result = email_verification.set_email_and_send(int(user["id"]), payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError:
        # Email provider configuration is a server-side concern. Never expose
        # SMTP/provider names, credentials, or configuration details to users.
        raise HTTPException(
            status_code=503,
            detail="Email delivery is temporarily unavailable. Please try again later.",
        )
    except Exception:
        logger.exception("Email verification delivery failed.")
        raise HTTPException(
            status_code=503,
            detail="Email delivery is temporarily unavailable. Please try again later.",
        )
    security_audit.record("email_verification_sent", username=str(user["username"]), success=True)
    return result


@app.post("/api/auth/email/resend")
def email_resend(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        result = email_verification.resend(int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Email delivery is temporarily unavailable. Please try again later.",
        )
    except Exception:
        logger.exception("Email verification resend failed.")
        raise HTTPException(
            status_code=503,
            detail="Email delivery is temporarily unavailable. Please try again later.",
        )
    if result.get("sent"):
        security_audit.record("email_verification_sent", username=str(user["username"]), success=True)
    return result


@app.get("/api/auth/email/verify")
def email_verify(token: str = Query(default="")) -> Response:
    try:
        email_verification.verify(token)
    except ValueError:
        return RedirectResponse(
            url=f"{PIPSGOX_WEB_URL}/?email_verified=failed",
            status_code=303,
        )
    return RedirectResponse(
        url=f"{PIPSGOX_WEB_URL}/?email_verified=success",
        status_code=303,
    )


@app.get("/api/auth/totp")
def totp_status(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return totp_service.status(int(user["id"]))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/totp/setup")
def totp_setup(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return totp_service.setup(int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/totp/confirm")
def totp_confirm(payload: TotpCodePayload, request: Request) -> dict[str, bool]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        totp_service.confirm_setup(int(user["id"]), payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    security_audit.record("totp_enable", username=str(user["username"]), success=True)
    return {"enabled": True}


@app.post("/api/auth/totp/disable")
def totp_disable(payload: TotpCodePayload, request: Request) -> dict[str, bool]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        totp_service.disable(int(user["id"]), payload.code)
        recovery_codes.invalidate_all(int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    security_audit.record("totp_disable", username=str(user["username"]), success=True)
    return {"enabled": False}


class PasskeyDeletePayload(BaseModel):
    passkey_id: int


@app.post("/api/auth/passkey/register/options")
def passkey_register_options(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return passkey_service.registration_options(int(user["id"]), request.headers.get("origin"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/passkey/register/verify")
def passkey_register_verify(
    payload: PasskeyCredentialPayload,
    request: Request,
) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        result = passkey_service.verify_registration(
            int(user["id"]),
            payload.credential,
            request.headers.get("origin"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    security_audit.record(
        "passkey_register",
        username=str(user["username"]),
        success=True,
    )
    return result


@app.post("/api/auth/passkey/login/options")
def passkey_login_options(request: Request) -> dict[str, object]:
    try:
        return passkey_service.authentication_options(request.headers.get("origin"))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/passkey/login/verify")
def passkey_login_verify(
    payload: PasskeyCredentialPayload,
    request: Request,
    response: Response,
) -> dict[str, object]:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    attempts, started = _LOGIN_ATTEMPTS.get(ip, (0, now))
    if now - started >= _LOGIN_WINDOW_SECONDS:
        attempts, started = 0, now
    if attempts >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    try:
        user_id, user = passkey_service.verify_authentication(payload.credential, request.headers.get("origin"))
        token = auth.create_session_for_user(
            user_id,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        _LOGIN_ATTEMPTS[ip] = (attempts + 1, started)
        security_audit.record("passkey_login", success=False)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _LOGIN_ATTEMPTS.pop(ip, None)
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
        path="/",
    )
    security_audit.record(
        "passkey_login",
        username=str(user["username"]),
        success=True,
    )
    return {"authenticated": True, **user}


@app.get("/api/auth/passkeys")
def passkey_list(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return {"passkeys": passkey_service.list_passkeys(int(user["id"]))}


@app.delete("/api/auth/passkeys/{passkey_id}")
def passkey_delete(passkey_id: int, request: Request) -> dict[str, bool]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        deleted = passkey_service.delete_passkey(int(user["id"]), passkey_id)
    except ValueError as exc:
        # This is an expected security-policy rejection, not a server error.
        security_audit.record(
            "passkey_delete",
            username=str(user["username"]),
            success=False,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Passkey not found.")

    security_audit.record(
        "passkey_delete",
        username=str(user["username"]),
        success=True,
    )
    return {"deleted": True}


@app.post("/api/security/disconnect-all")
def security_disconnect_all(request: Request) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    count = broker_accounts.disconnect_all()
    security_audit.record("broker_disconnect_all", username=str(user["username"]), success=True)
    return {"disconnected_accounts": count}


@app.post("/api/security/logout-all")
def security_logout_all(request: Request, response: Response) -> dict[str, object]:
    user = _request_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    count = auth.revoke_all(int(user["id"]))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    security_audit.record("logout_all_sessions", username=str(user["username"]), success=True)
    return {"revoked_sessions": count}


Timeframe = Literal["1m", "3m", "5m", "15m", "30m", "1h", "D", "W", "M"]


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int


class Quote(BaseModel):
    symbol: str
    exchange: str
    last: float
    change: float
    change_percent: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None
    source: str = "FYERS API V3"

class QuotesRequest(BaseModel):
    account_id: int | None = None
    symbols: list[str]


OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"]
OrderProduct = Literal["CNC", "INTRADAY", "MARGIN", "MTF"]
OrderValidity = Literal["DAY", "IOC"]


class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = "MARKET"
    product_type: OrderProduct = "INTRADAY"
    validity: OrderValidity = "DAY"
    price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int = 0
    after_market_order: bool = False
    correlation_id: str | None = None


class OrderModifyRequest(BaseModel):
    quantity: int
    order_type: OrderType = "MARKET"
    validity: OrderValidity = "DAY"
    price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int = 0


class PipscriptDataRequest(BaseModel):
    type: Literal["history", "quote", "quotes"]
    name: str | None = None
    symbol: str | None = None
    symbols: list[str] = []
    timeframe: Timeframe = "D"
    limit: int = 800
    from_date: date | None = None
    to_date: date | None = None


class PipscriptDataBatchRequest(BaseModel):
    account_id: int | None = None
    requests: list[PipscriptDataRequest] = []


class SymbolSearchResult(BaseModel):
    symbol: str
    name: str
    exchange: str
    api_symbol: str


provider = FyersMarketDataProvider(client_id="", access_token="")

# Pace Pipscript historical-data calls so a multi-index script does not
# burst past FYERS historical-data rate limits.
_pipscript_history_lock = threading.Lock()
_pipscript_last_history_request = 0.0
PIPSCRIPT_HISTORY_INTERVAL_SECONDS = 1.0


def _pipscript_get_history(
    api_symbol: str,
    timeframe: Timeframe,
    limit: int,
    *,
    start: date | None = None,
    end: date | None = None,
    pace: bool = True,
):
    global _pipscript_last_history_request

    if not pace:
        return provider.get_history(
            api_symbol,
            timeframe,
            limit,
            start=start,
            end=end,
        )

    with _pipscript_history_lock:
        now = time.monotonic()
        wait = PIPSCRIPT_HISTORY_INTERVAL_SECONDS - (now - _pipscript_last_history_request)
        if wait > 0:
            time.sleep(wait)

        result = provider.get_history(
            api_symbol,
            timeframe,
            limit,
            start=start,
            end=end,
        )
        _pipscript_last_history_request = time.monotonic()
        return result



_symbol_master_cache: dict[str, dict] = {}
_symbol_master_date: str | None = None
_symbol_master_lock = threading.Lock()
# Exact ticker -> FYERS API symbol mappings discovered from the master are cached
# so a BE/other-series stock is resolved once and stays fast afterward.
_symbol_resolution_cache: dict[str, list[str]] = {}


def _load_nse_symbol_master() -> dict[str, dict]:
    global _symbol_master_cache, _symbol_master_date

    today = __import__("datetime").date.today().isoformat()
    if _symbol_master_cache and _symbol_master_date == today:
        return _symbol_master_cache

    with _symbol_master_lock:
        if _symbol_master_cache and _symbol_master_date == today:
            return _symbol_master_cache

        try:
            response = requests.get(
                "https://public.fyers.in/sym_details/NSE_CM_sym_master.json",
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            if _symbol_master_cache:
                return _symbol_master_cache
            raise ValueError(f"Could not load FYERS NSE symbol master: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError("FYERS NSE symbol master returned an invalid payload.")

        _symbol_master_cache = payload
        _symbol_master_date = today
        _symbol_resolution_cache.clear()
        return _symbol_master_cache


def search_nse_symbols(query: str, limit: int = 12) -> list[SymbolSearchResult]:
    term = query.strip().upper()
    if not term:
        return []

    master = _load_nse_symbol_master()
    matches: list[tuple[int, SymbolSearchResult]] = []

    for api_symbol, item in master.items():
        if not isinstance(item, dict):
            continue

        ticker = str(item.get("symTicker") or "").strip().upper()
        name = str(item.get("exSymName") or item.get("symDetails") or "").strip()
        api = str(api_symbol or "").strip().upper()

        if not ticker or not api:
            continue

        haystack = f"{ticker} {name}".upper()
        if term not in haystack:
            continue

        if ticker == term:
            rank = 0
        elif ticker.startswith(term):
            rank = 1
        elif name.startswith(term):
            rank = 2
        else:
            rank = 3

        matches.append((
            rank,
            SymbolSearchResult(
                symbol=ticker,
                name=name or ticker,
                exchange="NSE",
                api_symbol=api,
            ),
        ))

    matches.sort(key=lambda item: (item[0], item[1].symbol, item[1].name))
    return [item[1] for item in matches[:limit]]


def resolve_api_symbol(symbol: str) -> str | None:
    """Resolve a user-facing symbol to an exact current FYERS API symbol.

    Normal NSE tickers must exist in the current FYERS symbol master. We do
    not fabricate an API symbol when the master has no matching instrument.
    """
    clean = symbol.strip().upper()
    if not clean:
        return None
    if ":" in clean:
        return clean

    # Index aliases must bypass the equity (-EQ) fallback. FYERS uses
    # explicit -INDEX symbols for NSE spot indices.
    alias_symbols = {
        "CNXMETAL", "CNXPHARMA", "NIFTY_IPO", "NIFTY_IND_DEFENCE",
        "NIFTY_HEALTHCARE", "NIFTY_CAPITAL_MKT", "CNXREALTY",
        "NIFTY_CONSR_DURBL", "NIFTY_EV", "NIFTY_TRANS_LOGIS",
        "CNXENERGY", "CNXAUTO", "CNXPSUBANK", "NIFTY_IND_DIGITAL",
        "NIFTYPVTBANK", "BANKNIFTY", "CNXCONSUMPTION", "CNXPSE",
        "CPSE", "NIFTY_IND_TOURISM", "CNXINFRA", "NIFTY_OIL_AND_GAS",
        "CNXFINANCE", "CNXSERVICE", "CNXIT", "CNXFMCG",
        "NIFTY_CEMENT", "NIFTY_CHEMICALS",
    }
    if clean in alias_symbols:
        return provider.symbol_info(clean).api_symbol

    # CSVs may already contain the FYERS series, e.g. MBECL-BE or MBECL-EQ.
    # Preserve that exact series instead of appending another -EQ suffix.
    if "-" in clean:
        base, series = clean.rsplit("-", 1)
        if base and series in {"EQ", "BE"}:
            return f"NSE:{base}-{series}"

    # Resolve ordinary NSE equities against the current FYERS master first.
    # This is important because many valid NSE stocks are currently in BE
    # rather than EQ (for example LOTUSDEV, SWANDEF and NURECA). Constructing
    # NSE:<SYMBOL>-EQ first can make a valid BE-only stock look unavailable.
    candidates = _master_symbol_candidates(clean)
    if candidates:
        return candidates[0]

    # Keep the deterministic EQ form as a temporary fallback if the daily
    # master is unavailable. The FYERS provider remains the authority on
    # whether that exact instrument exists.
    if clean.replace("&", "").replace("-", "").replace("_", "").isalnum():
        return provider.symbol_info(clean).api_symbol

    try:
        master = _load_nse_symbol_master()
    except ValueError:
        return provider.symbol_info(clean).api_symbol

    for api_symbol, item in master.items():
        if not isinstance(item, dict):
            continue

        api = str(api_symbol or "").strip().upper()
        ticker = str(item.get("symTicker") or "").strip().upper()
        exchange_symbol = str(item.get("exSymbol") or "").strip().upper()
        api_base = api.rsplit(":", 1)[-1].rsplit("-", 1)[0]

        if clean in {ticker, exchange_symbol, api_base} and api:
            return api

    return None


def _master_symbol_candidates(symbol: str) -> list[str]:
    """Return all supported current FYERS NSE symbols for a ticker.

    A ticker can exist in more than one exchange series. We prefer EQ, then BE,
    and keep the exact API symbols from FYERS' daily master instead of inventing
    a suffix. FYERS currently documents EQ and BE as the enabled NSE equity
    series; other restricted/SME series are not enabled for trading/data access.
    """
    clean = symbol.strip().upper()
    if not clean:
        return []

    # Explicit FYERS symbols are already authoritative.
    if ":" in clean:
        return [clean]

    # CSVs commonly contain "TICKER-EQ"/"TICKER-BE" without the exchange prefix.
    if "-" in clean:
        head, suffix = clean.rsplit("-", 1)
        if suffix in {"EQ", "BE"} and head:
            return [f"NSE:{head}-{suffix}"]

    with _symbol_master_lock:
        cached = _symbol_resolution_cache.get(clean)
    if cached:
        return list(cached)

    try:
        master = _load_nse_symbol_master()
    except ValueError:
        return []

    candidates: list[tuple[int, str]] = []
    for api_symbol, item in master.items():
        if not isinstance(item, dict):
            continue

        api = str(api_symbol or "").strip().upper()
        ticker = str(item.get("symTicker") or "").strip().upper()
        exchange_symbol = str(item.get("exSymbol") or "").strip().upper()
        if not api or not api.startswith("NSE:"):
            continue

        api_base = api.rsplit(":", 1)[-1]
        if "-" not in api_base:
            continue
        base, series = api_base.rsplit("-", 1)

        # The master has used slightly different identifier fields across
        # versions. Match the authoritative API key as well as ticker/name
        # fields, and tolerate a series suffix in symTicker/exSymbol.
        ticker_base = ticker.rsplit("-", 1)[0] if "-" in ticker else ticker
        exchange_base = (
            exchange_symbol.rsplit("-", 1)[0]
            if "-" in exchange_symbol
            else exchange_symbol
        )

        if clean not in {ticker, ticker_base, exchange_symbol, exchange_base, base}:
            continue

        # Prefer the two NSE equity series that FYERS currently enables.
        # Other NSE-CM records are retained after them so the resolver can
        # identify the exact master instrument instead of silently forcing EQ.
        priority = {"EQ": 0, "BE": 1}.get(series, 10)
        candidates.append((priority, api))

    candidates.sort(key=lambda item: (item[0], item[1]))
    symbols = [api for _, api in candidates]

    if symbols:
        with _symbol_master_lock:
            _symbol_resolution_cache[clean] = list(symbols)

    return symbols


def _resolve_api_symbol_from_master(symbol: str) -> str | None:
    candidates = _master_symbol_candidates(symbol)
    return candidates[0] if candidates else None


def unresolved_symbol_error(symbol: str) -> ValueError:
    return ValueError(
        f"Symbol '{symbol.strip().upper()}' was not found in the current FYERS NSE symbol master."
    )


def resolve_requested_symbols(symbols: list[str]) -> tuple[list[str], dict[str, str]]:
    api_symbols: list[str] = []
    api_to_original: dict[str, str] = {}

    for original in symbols:
        clean = original.strip().upper()
        api_symbol = resolve_api_symbol(clean)
        if not api_symbol:
            # Keep one bad/stale watchlist symbol from blocking all valid symbols.
            continue

        api_symbols.append(api_symbol)
        api_to_original[api_symbol.upper()] = clean

    return api_symbols, api_to_original

class FyersWatchlistStream:
    """Account-bound FYERS data socket for one PIPSGOX browser session."""

    def __init__(self, market_provider: FyersMarketDataProvider) -> None:
        self.provider = market_provider
        self._socket = None
        self._connect_lock = threading.Lock()
        self._symbol_lock = threading.Lock()
        self._subscribed: set[str] = set()
        self._client_symbols: dict[asyncio.Queue, set[str]] = {}
        self._api_to_app: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def _api_symbols(self, symbols: set[str]) -> set[str]:
        """Resolve all usable FYERS EQ/BE series for live watchlist data."""
        result: set[str] = set()
        with self._symbol_lock:
            for symbol in symbols:
                clean = symbol.strip().upper()
                if not clean:
                    continue

                candidates = _master_symbol_candidates(clean)
                if not candidates:
                    api_symbol = resolve_api_symbol(clean)
                    candidates = [api_symbol] if api_symbol else []

                for api_symbol in candidates:
                    suffix = api_symbol.rsplit("-", 1)[-1].upper()
                    if "-" in api_symbol and suffix not in {"EQ", "BE", "INDEX"}:
                        continue
                    result.add(api_symbol)
                    self._api_to_app[api_symbol.upper()] = clean
        return result

    def _connect(self) -> None:
        if not self.provider.configured:
            return
        with self._connect_lock:
            if self._socket is not None:
                return

            token = f"{self.provider.client_id}:{self.provider.access_token}"

            def on_message(message) -> None:
                if not isinstance(message, dict):
                    return

                api_symbol = str(message.get("symbol") or "").upper()
                if not api_symbol:
                    return

                with self._symbol_lock:
                    symbol = self._api_to_app.get(api_symbol)

                if not symbol:
                    return

                payload = {
                    "type": "quote",
                    "symbol": symbol,
                    "last": _float_value(message.get("ltp")),
                    "change": _float_value(message.get("ch")),
                    "change_percent": _float_value(message.get("chp")),
                    "open": _float_value(message.get("open_price")),
                    "high": _float_value(message.get("high_price")),
                    "low": _float_value(message.get("low_price")),
                    "volume": _int_value(message.get("vol_traded_today")),
                    "bid": _float_value(message.get("bid_price")),
                    "ask": _float_value(message.get("ask_price")),
                }

                loop = self._loop
                if loop and not loop.is_closed():
                    loop.call_soon_threadsafe(self._broadcast, payload)

            def on_error(message) -> None:
                loop = self._loop
                if loop and not loop.is_closed():
                    loop.call_soon_threadsafe(
                        self._broadcast,
                        {"type": "status", "status": "error", "message": str(message)},
                    )

            def on_close(message) -> None:
                self._socket = None
                loop = self._loop
                if loop and not loop.is_closed():
                    loop.call_soon_threadsafe(
                        self._broadcast,
                        {"type": "status", "status": "disconnected"},
                    )

            def on_connect() -> None:
                with self._symbol_lock:
                    symbols = set(self._subscribed)
                if symbols:
                    self._socket.subscribe(
                        symbols=sorted(symbols),
                        data_type="SymbolUpdate",
                    )
                self._socket.keep_running()

            self._socket = data_ws.FyersDataSocket(
                access_token=token,
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=on_connect,
                on_close=on_close,
                on_error=on_error,
                on_message=on_message,
            )

            self._socket.connect()

    async def update_client(self, queue: asyncio.Queue, symbols: list[str]) -> None:
        clean = {item.strip().upper() for item in symbols if item.strip()}
        if len(clean) > 1000:
            raise ValueError("A maximum of 1000 watchlist symbols is supported.")

        self._loop = asyncio.get_running_loop()
        previous = self._client_symbols.get(queue, set())
        self._client_symbols[queue] = clean

        union = set().union(*self._client_symbols.values()) if self._client_symbols else set()
        add = union - self._subscribed
        remove = self._subscribed - union
        self._subscribed = union

        self._api_symbols(union)

        if add or remove:
            if self._socket is None:
                asyncio.create_task(asyncio.to_thread(self._connect))
            else:
                api_add = self._api_symbols(add)
                api_remove = self._api_symbols(remove)
                if api_add:
                    await asyncio.to_thread(
                        self._socket.subscribe,
                        symbols=sorted(api_add),
                        data_type="SymbolUpdate",
                    )
                if api_remove:
                    await asyncio.to_thread(
                        self._socket.unsubscribe,
                        symbols=sorted(api_remove),
                        data_type="SymbolUpdate",
                    )

        if previous != clean:
            await queue.put({
                "type": "status",
                "status": "subscribed",
                "count": len(clean),
            })

    def remove_client(self, queue: asyncio.Queue) -> None:
        self._client_symbols.pop(queue, None)
        union = set().union(*self._client_symbols.values()) if self._client_symbols else set()
        remove = self._subscribed - union
        self._subscribed = union

        if self._socket is not None and remove:
            api_remove = self._api_symbols(remove)
            asyncio.create_task(asyncio.to_thread(
                self._socket.unsubscribe,
                symbols=sorted(api_remove),
                data_type="SymbolUpdate",
            ))

    def _broadcast(self, payload: dict) -> None:
        for queue in list(self._client_symbols):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)



def to_candle(item) -> Candle:
    return Candle(
        time=item.time,
        open=item.open,
        high=item.high,
        low=item.low,
        close=item.close,
        volume=item.volume,
    )


def to_quote(item) -> Quote:
    return Quote(
        symbol=item.symbol,
        exchange=item.exchange,
        last=item.last,
        change=item.change,
        change_percent=item.change_percent,
        open=item.open,
        high=item.high,
        low=item.low,
        volume=item.volume,
        bid=item.bid,
        ask=item.ask,
        source="FYERS API V3",
    )


@app.get("/auth/fyers/login")
def fyers_login() -> RedirectResponse:
    raise HTTPException(
        status_code=410,
        detail="Legacy FYERS login is disabled. Add and connect the broker from Account.",
    )


@app.get("/auth/fyers/callback")
def fyers_callback() -> RedirectResponse:
    raise HTTPException(
        status_code=410,
        detail="Legacy FYERS callback is disabled. Add and connect the broker from Account.",
    )


def _fyers_session_valid() -> bool:
    try:
        return any(
            account.broker == "fyers" and account.status == "connected"
            for account in broker_accounts.list_accounts()
        )
    except Exception:
        return False


def _system_readiness() -> dict[str, object]:
    """Return broker-agnostic PIPSGOX server/configuration readiness."""
    user_configured = auth.has_user()
    try:
        accounts = broker_accounts.list_accounts()
    except Exception:
        accounts = []

    connected_accounts = [
        account for account in accounts
        if account.status.strip().lower() == "connected"
    ]
    broker_accounts_configured = bool(accounts)
    broker_connected = bool(connected_accounts)
    ready = user_configured and broker_connected

    return {
        "user_configured": user_configured,
        "broker_accounts_configured": broker_accounts_configured,
        "broker_connected": broker_connected,
        "connected_broker_count": len(connected_accounts),
        "broker_account_count": len(accounts),
        "ready": ready,
    }




class BrokerAccountCreate(BaseModel):
    broker: str
    account_name: str
    client_id: str = ""
    api_secret: str
    api_key: str = ""


class BrokerAccountResponse(BaseModel):
    id: int
    broker: str
    account_name: str
    client_id: str
    status: str
    created_at: str
    updated_at: str


def _diagnostic_check(
    checks: list[dict[str, object]],
    *,
    check_id: str,
    label: str,
    category: str,
    component: str,
    severity: str,
    operation,
    error_code: str,
    account_id: int,
    broker: str,
    symbol: str = "",
) -> None:
    started = time.monotonic()
    try:
        operation()
    except Exception as exc:
        technical = diagnostics.sanitize(f"{type(exc).__name__}: {exc}")
        checks.append({
            "id": check_id,
            "label": label,
            "category": category,
            "component": component,
            "severity": severity,
            "status": "ERROR" if severity in {"CRITICAL", "ERROR"} else "WARNING",
            "error_code": error_code,
            "message": diagnostics.sanitize(str(exc) or "Check failed."),
            "technical_detail": technical,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "symbol": symbol,
        })
        diagnostics.record(
            severity=severity,
            category=category,
            component=component,
            service=label,
            error_code=error_code,
            message=str(exc) or "Check failed.",
            account_id=account_id,
            broker=broker,
            symbol=symbol,
            technical_detail=technical,
        )
        return

    checks.append({
        "id": check_id,
        "label": label,
        "category": category,
        "component": component,
        "severity": severity,
        "status": "OK",
        "message": "Check passed.",
        "technical_detail": "",
        "duration_ms": round((time.monotonic() - started) * 1000),
        "symbol": symbol,
    })
    diagnostics.resolve(error_code=error_code, account_id=account_id, symbol=symbol)


def _run_startup_checks(account_id: int) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    try:
        account, client_id, _, _ = broker_accounts.get_account_credentials(account_id)
        broker = account.broker.lower()
        access_token = broker_accounts.get_access_token(account_id)
    except Exception as exc:
        message = str(exc) or "Broker account could not be loaded."
        diagnostics.record(
            severity="CRITICAL",
            category="BROKER",
            component="Account",
            service="credentials",
            error_code="PIP-BROKER-ACCOUNT-001",
            message=message,
            account_id=account_id,
            technical_detail=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ready": False,
            "status": "FAILED",
            "account_id": account_id,
            "broker": "",
            "account_name": "",
            "checks": [{
                "id": "broker-account",
                "label": "Broker account",
                "category": "BROKER",
                "component": "Account",
                "severity": "CRITICAL",
                "status": "ERROR",
                "error_code": "PIP-BROKER-ACCOUNT-001",
                "message": message,
                "technical_detail": f"{type(exc).__name__}: {exc}",
            }],
            "summary": diagnostics.summary(account_id),
            "checked_at": time.time(),
        }

    if not access_token:
        message = "Broker account has no active access token. Reconnect the broker account."
        diagnostics.record(
            severity="CRITICAL",
            category="BROKER",
            component="Authentication",
            service="access token",
            error_code="PIP-BROKER-AUTH-001",
            message=message,
            account_id=account_id,
            broker=broker,
        )
        checks.append({
            "id": "broker-auth",
            "label": "Broker authentication",
            "category": "BROKER",
            "component": "Authentication",
            "severity": "CRITICAL",
            "status": "ERROR",
            "error_code": "PIP-BROKER-AUTH-001",
            "message": message,
            "technical_detail": "",
        })
        return {
            "ready": False,
            "status": "FAILED",
            "account_id": account_id,
            "broker": broker,
            "account_name": account.account_name,
            "checks": checks,
            "summary": diagnostics.summary(account_id),
            "checked_at": time.time(),
        }

    _diagnostic_check(
        checks, check_id="broker-session", label="Broker session",
        category="BROKER", component="Authentication", severity="CRITICAL",
        operation=lambda: BrokerManager.account_provider(account_id).validate(),
        error_code="PIP-BROKER-SESSION-001", account_id=account_id, broker=broker,
    )

    # Broker readiness checks are intentionally limited to trading/account APIs.
    # Market data is provided by the primary market-data master and is not part
    # of broker connection health, so an expired FYERS token cannot block charts.

    provider = None

    # These APIs are useful signals but do not block chart access.
    for check_id, label, method_name, error_code in (
        ("account-funds", "Funds API", "get_funds", "PIP-BROKER-FUNDS-001"),
        ("account-positions", "Positions API", "get_positions", "PIP-BROKER-POSITIONS-001"),
        ("account-holdings", "Holdings API", "get_holdings", "PIP-BROKER-HOLDINGS-001"),
        ("account-orders", "Orders API", "get_orders", "PIP-BROKER-ORDERS-001"),
    ):
        _diagnostic_check(
            checks, check_id=check_id, label=label, category="ACCOUNT",
            component=label.replace(" API", ""), severity="WARNING",
            operation=lambda method_name=method_name: getattr(
                provider or BrokerManager.account_provider(account_id), method_name
            )(),
            error_code=error_code, account_id=account_id, broker=broker,
        )

    checks.append({
        "id": "market-data-master",
        "label": "Primary market data",
        "category": "MARKET_DATA",
        "component": "Market Data Master",
        "severity": "INFO",
        "status": "OK",
        "message": f"Market data is independent from broker connection and uses {configured_source()}.",
        "technical_detail": "No broker/FYERS market-data request is made during broker startup checks.",
    })

    blocking = [
        item for item in checks
        if item.get("severity") == "CRITICAL" and item.get("status") == "ERROR"
    ]
    warnings = [item for item in checks if item.get("status") == "WARNING"]
    status = "FAILED" if blocking else "DEGRADED" if warnings else "HEALTHY"

    if blocking:
        diagnostics.record(
            severity="CRITICAL", category="STARTUP", component="Readiness Gate",
            service="startup", error_code="PIP-STARTUP-001",
            message="PIPSGOX startup checks found one or more blocking failures.",
            account_id=account_id, broker=broker,
        )
    else:
        diagnostics.resolve(error_code="PIP-STARTUP-001", account_id=account_id)

    return {
        "ready": not blocking,
        "status": status,
        "account_id": account_id,
        "broker": broker,
        "account_name": account.account_name,
        "checks": checks,
        "summary": diagnostics.summary(account_id),
        "checked_at": time.time(),
    }


class FrontendDiagnosticRequest(BaseModel):
    account_id: int | None = None
    message: str
    stack: str = ""
    component_stack: str = ""


@app.post("/api/diagnostics/frontend")
def diagnostics_frontend(request: FrontendDiagnosticRequest) -> dict[str, object]:
    event = diagnostics.record(
        severity="CRITICAL",
        category="FRONTEND",
        component="React",
        service="frontend-renderer",
        error_code="PIP-FRONTEND-RENDER-001",
        message=request.message,
        account_id=request.account_id,
        technical_detail=f"{request.stack}\n{request.component_stack}".strip(),
    )
    return {"recorded": True, "event_id": event.get("id")}


@app.post("/api/startup/check/{account_id}")
def startup_check(account_id: int) -> dict[str, object]:
    return _run_startup_checks(account_id)


@app.get("/api/diagnostics/summary")
def diagnostics_summary(account_id: int | None = Query(default=None, ge=1)) -> dict[str, object]:
    return diagnostics.summary(account_id)


@app.get("/api/diagnostics/events")
def diagnostics_events(
    account_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    include_resolved: bool = True,
) -> list[dict[str, object]]:
    return diagnostics.list_events(account_id=account_id, limit=limit, include_resolved=include_resolved)


def _broker_account_response(account) -> BrokerAccountResponse:
    return BrokerAccountResponse(
        id=account.id,
        broker=account.broker,
        account_name=account.account_name,
        client_id=broker_accounts.mask_client_id(account.client_id),
        status=account.status,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@app.get("/api/broker/accounts", response_model=list[BrokerAccountResponse])
def broker_accounts_list() -> list[BrokerAccountResponse]:
    return [_broker_account_response(account) for account in broker_accounts.list_accounts()]


@app.post("/api/broker/accounts", response_model=BrokerAccountResponse, status_code=201)
def broker_accounts_create(payload: BrokerAccountCreate) -> BrokerAccountResponse:
    broker = payload.broker.strip().lower()
    if broker == "fyers" and not payload.api_key.strip():
        raise HTTPException(status_code=400, detail="FYERS App ID is required.")
    if broker == "dhan" and (not payload.client_id.strip() or not payload.api_key.strip()):
        raise HTTPException(status_code=400, detail="Dhan Client ID and API Key are required.")
    try:
        account = broker_accounts.create_account(
            broker, payload.account_name, payload.client_id, payload.api_secret, payload.api_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _broker_account_response(account)



@app.get("/api/broker/accounts/{account_id}/connect")
def broker_account_connect(account_id: int, request: Request, response: Response) -> dict[str, str]:
    try:
        account, client_id, api_key, api_secret = broker_accounts.get_account_credentials(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    app_session = request.cookies.get(auth.SESSION_COOKIE)
    if not app_session:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session_hash = hashlib.sha256(app_session.encode("utf-8")).hexdigest()
    state = secrets.token_urlsafe(24)

    try:
        if account.broker == "fyers":
            # Persist FYERS OAuth state so a backend restart during the
            # external login cannot invalidate the callback.
            broker_accounts.create_oauth_state(
                state,
                account.id,
                account.broker,
                session_hash,
                int(time.time()) + 600,
            )
            # FYERS OAuth identifies the API application with the App ID.
            # The trading Client ID is not used for this parameter.
            fyers_app_id = api_key.strip() or client_id.strip()
            result = BrokerManager.start_fyers(
                account.id,
                fyers_app_id,
                f"{_codespace_forwarded_url(8000)}/auth/broker/fyers/callback",
                state,
            )
        elif account.broker == "dhan":
            login_state = secrets.token_urlsafe(32)
            app_session = request.cookies.get(auth.SESSION_COOKIE)
            if not app_session:
                raise HTTPException(status_code=401, detail="Authentication required.")
            broker_accounts.create_oauth_state(
                login_state,
                account.id,
                account.broker,
                hashlib.sha256(app_session.encode("utf-8")).hexdigest(),
                int(time.time()) + 600,
            )
            result = BrokerManager.start_dhan(account.id, client_id, api_key, api_secret)
        else:
            raise ValueError(f"Unsupported broker '{account.broker}'.")
    except ValueError as exc:
        # OAuth state is persisted and will expire naturally.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if account.broker == "fyers":
        # The web UI (3001) and OAuth callback (8000) are different hosts in
        # Codespaces, so the host-only app session cookie is not available at
        # the callback. Bind the OAuth transaction to a short-lived, HttpOnly
        # cookie on the API host instead.
        response.set_cookie(
            "pipsgox_fyers_state",
            state,
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite="lax",
            max_age=600,
            path="/",
        )
    else:
        response.set_cookie(
            "pipsgox_dhan_state",
            login_state,
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite="lax",
            max_age=600,
            path="/",
        )
    return {"broker": result.broker, "authorization_url": result.authorization_url}


@app.get("/auth/broker/fyers/callback")
def broker_fyers_callback(
    auth_code: str | None = None,
    state: str | None = None,
    request: Request = None,
) -> RedirectResponse:
    if not auth_code or not state:
        raise HTTPException(status_code=400, detail="FYERS did not return a valid authorization response.")
    context = broker_accounts.consume_oauth_state(state)
    if not context or context[1] != "fyers":
        raise HTTPException(status_code=400, detail="Invalid or expired broker login state.")

    account_id = context[0]
    try:
        _, client_id, api_key, api_secret = broker_accounts.get_account_credentials(account_id)
        fyers_app_id = api_key.strip() or client_id.strip()
        token = BrokerManager.exchange_fyers_code(fyers_app_id, api_secret, auth_code)
        BrokerManager.validate_fyers_token(fyers_app_id, token)
        broker_accounts.set_access_token(account_id, token, "connected")
    except ValueError:
        broker_accounts.set_status(account_id, "error")
        return RedirectResponse(
            url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id) + "&oauth_error=1",
            status_code=303,
        )
    except Exception:
        broker_accounts.set_status(account_id, "error")
        return RedirectResponse(
            url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id) + "&oauth_error=1",
            status_code=303,
        )

    redirect = RedirectResponse(
        url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id),
        status_code=303,
    )
    redirect.delete_cookie("pipsgox_fyers_state", path="/")
    return redirect


@app.get("/auth/broker/dhan/callback")
def broker_dhan_callback(token_id: str | None = None, request: Request = None) -> RedirectResponse:
    state = request.cookies.get("pipsgox_dhan_state") if request else None
    context = broker_accounts.consume_oauth_state(state) if state else None

    # Dhan does not return our application state parameter. Prefer the
    # short-lived state cookie, but recover the pending transaction from the
    # authenticated PIPSGOX session when the proxy/browser does not preserve
    # the auxiliary state cookie across the OAuth redirect.
    if not context and request:
        app_session = request.cookies.get(auth.SESSION_COOKIE)
        if app_session:
            session_hash = hashlib.sha256(app_session.encode("utf-8")).hexdigest()
            context = broker_accounts.consume_latest_oauth_state_for_session(
                "dhan", session_hash
            )

    account_id = context[0] if context else None
    valid_state = bool(context and context[1] == "dhan")
    if not token_id or account_id is None or not valid_state:
        raise HTTPException(status_code=400, detail="Dhan login state is missing, invalid, or expired.")
    try:
        account, client_id, api_key, api_secret = broker_accounts.get_account_credentials(account_id)
        if account.broker != "dhan":
            raise ValueError("Selected account is not a Dhan account.")
        token = BrokerManager.exchange_dhan_token(api_key, api_secret, token_id)
        BrokerManager.validate_dhan_token(client_id, token)
        broker_accounts.set_access_token(account_id, token, "connected")
    except ValueError:
        broker_accounts.set_status(account_id, "error")
        return RedirectResponse(
            url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id) + "&oauth_error=1",
            status_code=303,
        )
    except Exception:
        broker_accounts.set_status(account_id, "error")
        return RedirectResponse(
            url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id) + "&oauth_error=1",
            status_code=303,
        )
    redirect = RedirectResponse(
        url=PIPSGOX_WEB_URL.rstrip("/") + "/startup?account_id=" + str(account_id),
        status_code=303,
    )
    redirect.delete_cookie("pipsgox_dhan_state", path="/")
    return redirect


@app.get("/api/broker/accounts/{account_id}/session")
def broker_account_session(account_id: int) -> dict[str, object]:
    try:
        account, client_id, api_key, _ = broker_accounts.get_account_credentials(account_id)
        token = broker_accounts.get_access_token(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    connected = False
    if token and account.broker == "fyers":
        try:
            BrokerManager.validate_fyers_token(api_key.strip() or client_id.strip(), token)
            connected = True
            broker_accounts.set_status(account_id, "connected")
        except Exception:
            broker_accounts.set_status(account_id, "expired")
    elif token and account.broker == "dhan":
        try:
            BrokerManager.validate_dhan_token(client_id, token)
            connected = True
            broker_accounts.set_status(account_id, "connected")
        except Exception:
            broker_accounts.set_status(account_id, "expired")

    return {"id": account.id, "broker": account.broker, "status": "connected" if connected else account.status}


def _broker_account_provider(account_id: int):
    try:
        return BrokerManager.account_provider(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _safe_broker_error(account_id: int, operation: str, provider: object, exc: Exception, *, username: str = "") -> HTTPException:
    message = str(exc).lower()
    if any(marker in message for marker in ("401", "403", "unauthorized", "access token", "session expired", "token expired")):
        broker_accounts.set_status(account_id, "expired")
        security_audit.record(operation, username=username, account_id=account_id, broker=str(getattr(provider, "broker", "")), success=False)
        return HTTPException(status_code=409, detail="Broker session expired. Reconnect this account.")
    broker_accounts.set_status(account_id, "error")
    security_audit.record(operation, username=username, account_id=account_id, broker=str(getattr(provider, "broker", "")), success=False)
    return HTTPException(status_code=502, detail="Broker request failed.")

def _verify_order_belongs_to_account(provider: object, order_id: str) -> None:
    from app.providers.accounts import normalize_order, _normalize_broker_items
    wanted = order_id.strip()
    if not wanted or len(wanted) > 128:
        raise HTTPException(status_code=400, detail="Invalid order ID.")
    items = _normalize_broker_items(provider.get_orders(), "orderBook", "orders")
    for item in items:
        if normalize_order(provider, item).get("order_id") == wanted:
            return
    raise HTTPException(status_code=404, detail="Order was not found in the selected broker account.")

@app.post("/api/broker/accounts/{account_id}/orders")
def broker_account_place_order(account_id: int, request: OrderRequest, http_request: Request) -> dict[str, object]:
    if request.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero.")
    if request.order_type == "LIMIT" and (request.price is None or request.price <= 0):
        raise HTTPException(status_code=400, detail="A positive price is required for LIMIT orders.")
    if request.order_type in {"STOP_LOSS", "STOP_LOSS_MARKET"} and (request.trigger_price is None or request.trigger_price <= 0):
        raise HTTPException(status_code=400, detail="A positive trigger price is required for stop orders.")

    correlation_id = (request.correlation_id or "").strip()
    if correlation_id and len(correlation_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid order correlation ID.")

    provider = _broker_account_provider(account_id)
    user = _request_user(http_request)
    if correlation_id and not broker_accounts.claim_order_correlation(account_id, correlation_id):
        raise HTTPException(status_code=409, detail="Duplicate order request blocked.")
    try:
        data = provider.place_order(request.model_dump())
        security_audit.record("order_place", username=str(user["username"]) if user else "", account_id=account_id, broker=provider.broker, success=True)
        return {"account_id": account_id, "broker": provider.broker, "data": data}
    except ValueError as exc:
        security_audit.record("order_place", username=str(user["username"]) if user else "", account_id=account_id, broker=provider.broker, success=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _safe_broker_error(account_id, "order_place", provider, exc, username=str(user["username"]) if user else "") from exc

@app.put("/api/broker/accounts/{account_id}/orders/{order_id}")
def broker_account_modify_order(account_id: int, order_id: str, request: OrderModifyRequest, http_request: Request) -> dict[str, object]:
    if request.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero.")
    if request.order_type == "LIMIT" and (request.price is None or request.price <= 0):
        raise HTTPException(status_code=400, detail="A positive price is required for LIMIT orders.")
    if request.order_type in {"STOP_LOSS", "STOP_LOSS_MARKET"} and (request.trigger_price is None or request.trigger_price <= 0):
        raise HTTPException(status_code=400, detail="A positive trigger price is required for stop orders.")

    provider = _broker_account_provider(account_id)
    user = _request_user(http_request)
    try:
        _verify_order_belongs_to_account(provider, order_id)
        data = provider.modify_order(order_id, request.model_dump())
        security_audit.record("order_modify", username=str(user["username"]) if user else "", account_id=account_id, broker=provider.broker, success=True)
        return {"account_id": account_id, "broker": provider.broker, "data": data}
    except HTTPException:
        raise
    except ValueError as exc:
        security_audit.record("order_modify", username=str(user["username"]) if user else "", account_id=account_id, broker=provider.broker, success=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _safe_broker_error(account_id, "order_modify", provider, exc, username=str(user["username"]) if user else "") from exc

@app.delete("/api/broker/accounts/{account_id}/orders/{order_id}")
def broker_account_cancel_order(account_id: int, order_id: str, http_request: Request) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    user = _request_user(http_request)
    try:
        _verify_order_belongs_to_account(provider, order_id)
        data = provider.cancel_order(order_id)
        security_audit.record("order_cancel", username=str(user["username"]) if user else "", account_id=account_id, broker=provider.broker, success=True)
        return {"account_id": account_id, "broker": provider.broker, "data": data}
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_broker_error(account_id, "order_cancel", provider, exc, username=str(user["username"]) if user else "") from exc


def _normalize_funds(provider: object, raw: object) -> dict[str, object]:
    """Normalize broker-specific fund responses into one UI-facing schema."""
    payload = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
    if not isinstance(payload, dict):
        raise ValueError("Broker returned an invalid funds response.")

    def number(*keys: str) -> float | None:
        for key in keys:
            value = payload.get(key)
            try:
                result = float(value)
            except (TypeError, ValueError):
                continue
            if result == result:
                return result
        return None

    broker = str(getattr(provider, "broker", "")).lower()
    if broker == "dhan":
        return {
            "available": number("availabelBalance", "availableBalance"),
            "sod_limit": number("sodLimit"),
            "utilized": number("utilizedAmount"),
            "collateral": number("collateralAmount"),
            "receivable": number("receiveableAmount", "receivableAmount"),
            "withdrawable": number("withdrawableBalance"),
            "currency": "INR",
            "broker": "dhan",
        }

    # FYERS commonly returns fund_limit as a list of account-level limits.
    limits = payload.get("fund_limit")
    if isinstance(limits, list):
        merged: dict[str, object] = {}
        for item in limits:
            if isinstance(item, dict):
                merged.update(item)
        payload = {**payload, **merged}

    return {
        "available": number("avail_cash", "availableBalance", "available"),
        "sod_limit": number("fund_limit", "fund_limit_total", "sodLimit"),
        "utilized": number("utilized_amount", "utilizedAmount", "utilized"),
        "collateral": number("collateral", "collateralAmount"),
        "receivable": number("receivable", "receiveableAmount"),
        "withdrawable": number("withdrawable_balance", "withdrawableBalance"),
        "currency": "INR",
        "broker": "fyers",
    }


@app.get("/api/broker/accounts/{account_id}/funds")
def broker_account_funds(account_id: int) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    try:
        return {
            "account_id": account_id,
            "broker": provider.broker,
            "data": _normalize_funds(provider, provider.get_funds()),
        }
    except Exception as exc:
        broker_accounts.set_status(account_id, "error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/broker/accounts/{account_id}/positions")
def broker_account_positions(account_id: int) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    try:
        from app.providers.accounts import normalize_position
        raw = provider.get_positions()
        items = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            items = []
        positions = [
            normalize_position(provider, item)
            for item in items
            if isinstance(item, dict)
        ]
        return {
            "account_id": account_id,
            "broker": provider.broker,
            "data": positions,
        }
    except Exception as exc:
        broker_accounts.set_status(account_id, "error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/broker/accounts/{account_id}/orders")
def broker_account_orders(account_id: int) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    try:
        from app.providers.accounts import normalize_order, _normalize_broker_items
        items = _normalize_broker_items(provider.get_orders(), "orderBook", "orders")
        return {
            "account_id": account_id,
            "broker": provider.broker,
            "data": [normalize_order(provider, item) for item in items],
        }
    except Exception as exc:
        broker_accounts.set_status(account_id, "error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/broker/accounts/{account_id}/trades")
def broker_account_trades(account_id: int) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    try:
        from app.providers.accounts import normalize_trade, _normalize_broker_items
        items = _normalize_broker_items(provider.get_trades(), "tradeBook", "trades")
        return {
            "account_id": account_id,
            "broker": provider.broker,
            "data": [normalize_trade(provider, item) for item in items],
        }
    except Exception as exc:
        broker_accounts.set_status(account_id, "error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/broker/accounts/{account_id}/holdings")
def broker_account_holdings(account_id: int) -> dict[str, object]:
    provider = _broker_account_provider(account_id)
    try:
        from app.providers.accounts import normalize_holding, _normalize_broker_items
        items = _normalize_broker_items(provider.get_holdings(), "holdings", "data")
        return {
            "account_id": account_id,
            "broker": provider.broker,
            "data": [normalize_holding(provider, item) for item in items],
        }
    except Exception as exc:
        broker_accounts.set_status(account_id, "error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.delete("/api/broker/accounts/{account_id}")
def broker_accounts_delete(account_id: int) -> dict[str, bool]:
    if not broker_accounts.delete_account(account_id):
        raise HTTPException(status_code=404, detail="Broker account not found.")
    return {"deleted": True}


@app.get("/api/fyers/status")
def fyers_status() -> dict[str, object]:
    connected = _fyers_session_valid()
    return {
        "configured": connected,
        "connected": connected,
        "status": "connected" if connected else "not_connected",
        "redirect_uri": FYERS_REDIRECT_URI,
    }


@app.get("/health")
def health() -> dict[str, object]:
    readiness = _system_readiness()
    return {
        "status": "ok",
        "app": "pipsgox",
        "server": "running",
        "data_provider": configured_source(),
        **readiness,
    }


@app.get("/api/scanner/44ma")
def ma44_scanner_status(
    mode: Literal["live", "eod"] = "live",
    account_id: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    return ma44_scanner.snapshot(mode, account_id=account_id)


@app.get("/api/symbols/search", response_model=list[SymbolSearchResult])
def symbol_search(
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=12, ge=1, le=25),
) -> list[SymbolSearchResult]:
    try:
        return search_nse_symbols(q, limit)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _aggregate_daily_candles(candles: list[Candle], period: str) -> list[Candle]:
    import datetime as _dt
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        dt = _dt.datetime.fromtimestamp(candle.time, tz=_dt.timezone.utc)
        key = (dt.year, dt.isocalendar().week) if period == "W" else (dt.year, dt.month)
        groups.setdefault(key, []).append(candle)

    result: list[Candle] = []
    for items in groups.values():
        items.sort(key=lambda item: item.time)
        result.append(Candle(
            time=items[0].time,
            open=items[0].open,
            high=max(item.high for item in items),
            low=min(item.low for item in items),
            close=items[-1].close,
            volume=sum(item.volume for item in items),
        ))
    return sorted(result, key=lambda item: item.time)


def _aggregate_intraday_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    if minutes <= 1:
        return candles
    bucket_seconds = minutes * 60
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        bucket = candle.time - (candle.time % bucket_seconds)
        groups.setdefault(bucket, []).append(candle)

    result: list[Candle] = []
    for bucket, items in groups.items():
        items.sort(key=lambda item: item.time)
        result.append(Candle(
            time=bucket,
            open=items[0].open,
            high=max(item.high for item in items),
            low=min(item.low for item in items),
            close=items[-1].close,
            volume=sum(item.volume for item in items),
        ))
    return sorted(result, key=lambda item: item.time)


def _dhan_history_to_candles(payload: dict[str, object]) -> list[Candle]:
    timestamps = payload.get("timestamp") or payload.get("timestamps") or []
    opens = payload.get("open") or []
    highs = payload.get("high") or []
    lows = payload.get("low") or []
    closes = payload.get("close") or []
    volumes = payload.get("volume") or []

    if not all(isinstance(value, list) for value in (timestamps, opens, highs, lows, closes, volumes)):
        raise ValueError("Dhan historical response has an invalid candle structure.")

    count = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    candles: list[Candle] = []
    for index in range(count):
        try:
            timestamp = int(float(timestamps[index]))
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            candles.append(Candle(
                time=timestamp,
                open=float(opens[index]),
                high=float(highs[index]),
                low=float(lows[index]),
                close=float(closes[index]),
                volume=float(volumes[index]) if index < len(volumes) else 0.0,
            ))
        except (TypeError, ValueError):
            continue
    return candles


def _dhan_history(
    account_id: int,
    symbol: str,
    timeframe: Timeframe,
    limit: int,
    from_date: date | None,
    to_date: date | None,
) -> list[Candle]:
    account, client_id, _, _ = broker_accounts.get_account_credentials(account_id)
    access_token = broker_accounts.get_access_token(account_id)
    if account.broker != "dhan":
        raise ValueError("Selected account is not a Dhan account.")
    if not access_token:
        raise HTTPException(status_code=409, detail="Selected Dhan account is not connected.")

    from app.providers.accounts import DhanAccountProvider
    dhan = DhanAccountProvider(client_id, access_token)
    instrument = dhan.resolve_instrument(symbol)
    if not instrument or not instrument.get("security_id"):
        raise ValueError(f"Dhan instrument not found for {symbol}.")

    import datetime as _dt
    end = to_date or _dt.date.today()
    if from_date:
        start = from_date
    elif timeframe == "D":
        start = end - _dt.timedelta(days=max(limit * 2, 30))
    else:
        start = end - _dt.timedelta(days=min(max(limit * 2, 10), 90))

    if timeframe in {"D", "W", "M"}:
        payload = dhan.get_history(
            instrument["security_id"],
            instrument["exchange_segment"],
            instrument["instrument"],
            "D",
            start.isoformat(),
            end.isoformat(),
        )
        candles = _dhan_history_to_candles(payload)
        if timeframe == "W":
            return _aggregate_daily_candles(candles, "W")[-limit:]
        if timeframe == "M":
            return _aggregate_daily_candles(candles, "M")[-limit:]
        return candles[-limit:]

    if timeframe == "30m":
        base_timeframe = "15m"
    elif timeframe == "3m":
        base_timeframe = "1m"
    else:
        base_timeframe = timeframe

    payload = dhan.get_history(
        instrument["security_id"],
        instrument["exchange_segment"],
        instrument["instrument"],
        base_timeframe,
        start.isoformat() + " 09:15:00",
        end.isoformat() + " 15:30:00",
    )
    candles = _dhan_history_to_candles(payload)
    if timeframe in {"30m", "3m"}:
        candles = _aggregate_intraday_candles(candles, int(timeframe[:-1]))
    return candles[-limit:]



class _DhanMarketDataAdapter:
    """Broker-neutral market-data adapter for Dhan."""
    broker = "dhan"

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None):
        return _dhan_history(self.account_id, symbol, timeframe, limit, start, end)

    def get_quote(self, symbol: str):
        return _dhan_quote(self.account_id, symbol)

    def get_quotes(self, symbols: list[str]):
        return [self.get_quote(symbol) for symbol in symbols]


@app.get("/api/market-data/source")
def market_data_source() -> dict[str, object]:
    source = configured_source()
    return {
        "source": source,
        "available_sources": ["yfinance", "nse", "bse"],
        "broker_independent": True,
    }


def _market_data_provider_for_account(account_id: int | None):

    if account_id is None:
        raise HTTPException(status_code=409, detail="Select a connected broker account before loading market data.")
    try:
        account, client_id, api_key, _ = broker_accounts.get_account_credentials(account_id)
        access_token = broker_accounts.get_access_token(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not access_token:
        raise HTTPException(status_code=409, detail="Selected broker account is not connected.")

    # Every broker exposes the same market-data contract to the terminal.
    # Broker-specific credential/instrument details stay inside this factory.
    # FYERS API calls use the App ID (stored as api_key), not the trading
    # Client ID. The access token is bound to that App ID.
    market_adapters = {
        "fyers": lambda: FyersMarketDataProvider(
            client_id=api_key.strip() or client_id.strip(),
            access_token=access_token,
        ),
        "dhan": lambda: _DhanMarketDataAdapter(account_id),
    }
    factory = market_adapters.get(account.broker.lower())
    if not factory:
        raise HTTPException(status_code=501, detail=f"Market data is not supported for broker '{account.broker}'.")
    return factory()


@app.get("/api/history", response_model=list[Candle])
def history(
    symbol: str = Query(default="BHARTIARTL", min_length=1, max_length=40),
    timeframe: Timeframe = "D",
    limit: int = Query(default=800, ge=50, le=2000),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    account_id: int | None = Query(default=None, ge=1),
) -> list[Candle]:
    clean_symbol = symbol.strip().upper()
    source = configured_source()
    try:
        candles = market_data_master.get_history(
            clean_symbol,
            timeframe,
            limit,
            start=from_date,
            end=to_date,
        )
    except Exception as exc:
        logger.exception(
            "Primary market-data history failed: source=%s symbol=%s timeframe=%s",
            source, clean_symbol, timeframe,
        )
        diagnostics.record(
            severity="ERROR",
            category="MARKET_DATA",
            component="Primary Market Data",
            service="/api/history",
            error_code="PIP-MARKET-MASTER-001",
            message=f"{source} history failed for {clean_symbol}.",
            symbol=clean_symbol,
            technical_detail=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=f"{source} market data failed for {clean_symbol}: {exc}",
        ) from exc

    if not candles:
        diagnostics.record(
            severity="ERROR",
            category="MARKET_DATA",
            component="Primary Market Data",
            service="/api/history",
            error_code="PIP-MARKET-MASTER-002",
            message=f"No historical data returned for {clean_symbol}.",
            symbol=clean_symbol,
        )
        raise HTTPException(
            status_code=503,
            detail=f"No historical data returned for {clean_symbol}.",
        )

    diagnostics.resolve(error_code="PIP-MARKET-MASTER-001", symbol=clean_symbol)
    diagnostics.resolve(error_code="PIP-MARKET-MASTER-002", symbol=clean_symbol)
    return [to_candle(item) for item in candles]


@app.post("/api/pipscript/data")
def pipscript_data(request: PipscriptDataBatchRequest) -> dict[str, object]:
    """Controlled market-data gateway for browser Pipscripts.

    Pipscripts use the same primary market-data source as charts and watchlists.
    Broker connections are never required for market-data requests.
    """
    if len(request.requests) > 40:
        raise HTTPException(status_code=400, detail="A maximum of 40 Pipscript data requests is supported.")

    history_data: dict[str, dict[str, object]] = {}
    quotes_data: dict[str, object] = {}
    errors: list[dict[str, str]] = []

    for index, item in enumerate(request.requests):
        name = (item.name or item.symbol or f"request_{index + 1}").strip().upper() or f"REQUEST_{index + 1}"
        try:
            if item.type == "history":
                if not item.symbol:
                    raise ValueError("history request requires symbol.")
                if not 50 <= item.limit <= 2000:
                    raise ValueError("history limit must be between 50 and 2000.")
                original = item.symbol.strip().upper()
                candles = market_data_master.get_history(
                    original,
                    item.timeframe,
                    item.limit,
                    start=item.from_date,
                    end=item.to_date,
                )
                history_data[name] = {
                    "symbol": original,
                    "timeframe": item.timeframe,
                    "bars": [to_candle(candle).model_dump() for candle in candles],
                }
            elif item.type == "quote":
                if not item.symbol:
                    raise ValueError("quote request requires symbol.")
                original = item.symbol.strip().upper()
                result = market_data_master.get_quote(original)
                quotes_data[name] = to_quote(result).model_dump()
            elif item.type == "quotes":
                requested = [value.strip().upper() for value in item.symbols if value and value.strip()]
                if not requested:
                    raise ValueError("quotes request requires a non-empty symbols array.")
                if len(requested) > 1000:
                    raise ValueError("A maximum of 1000 symbols can be requested in one quotes request.")
                results = market_data_master.get_quotes(requested)
                quotes_data[name] = {"items": [quote.model_dump() for quote in results]}
        except (ValueError, HTTPException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            errors.append({"name": name, "type": item.type, "message": str(message)})

    if errors:
        severity = "ERROR" if len(errors) == len(request.requests) and request.requests else "WARNING"
        diagnostics.record(
            severity=severity,
            category="PIPSCRIPT",
            component="Data Gateway",
            service="/api/pipscript/data",
            error_code="PIP-PIPSCRIPT-DATA-001",
            message=f"{len(errors)} Pipscript data request(s) failed.",
            technical_detail=json.dumps(errors, ensure_ascii=False),
        )
    else:
        diagnostics.resolve(error_code="PIP-PIPSCRIPT-DATA-001")
    return {"history": history_data, "quotes": quotes_data, "errors": errors}


def _dhan_quote(account_id: int, symbol: str) -> Quote:
    account, client_id, _, _ = broker_accounts.get_account_credentials(account_id)
    access_token = broker_accounts.get_access_token(account_id)
    if account.broker != "dhan":
        raise ValueError("Selected account is not a Dhan account.")
    if not access_token:
        raise HTTPException(status_code=409, detail="Selected Dhan account is not connected.")

    from app.providers.accounts import DhanAccountProvider
    dhan = DhanAccountProvider(client_id, access_token)
    instrument = dhan.resolve_instrument(symbol)
    if not instrument or not instrument.get("security_id"):
        raise ValueError(f"Dhan instrument not found for {symbol}.")
    payload = dhan.get_ltp({"NSE_EQ": [int(instrument["security_id"])]})
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    segment = data.get(instrument["exchange_segment"], {}) if isinstance(data, dict) else {}
    item = segment.get(instrument["security_id"], {}) if isinstance(segment, dict) else {}
    if not isinstance(item, dict) or item.get("last_price") is None:
        raise ValueError(f"Dhan returned no LTP for {symbol}.")
    return Quote(
        symbol=symbol.upper(),
        exchange="NSE",
        last=float(item["last_price"]),
        change=0.0,
        change_percent=0.0,
        source="DhanHQ API V2",
    )


@app.get("/api/quote", response_model=Quote)
def quote(
    symbol: str = Query(default="BHARTIARTL", min_length=1, max_length=40),
    account_id: int | None = Query(default=None, ge=1),
) -> Quote:
    original = symbol.strip().upper()
    source = configured_source()
    try:
        result = market_data_master.get_quote(original)
    except Exception as exc:
        logger.exception(
            "Primary market-data quote failed: source=%s symbol=%s",
            source, original,
        )
        diagnostics.record(
            severity="ERROR",
            category="MARKET_DATA",
            component="Primary Market Data",
            service="/api/quote",
            error_code="PIP-MARKET-MASTER-003",
            message=f"{source} quote failed for {original}.",
            symbol=original,
            technical_detail=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=f"{source} market data quote failed for {original}: {exc}",
        ) from exc

    if result.last <= 0:
        diagnostics.record(
            severity="ERROR",
            category="MARKET_DATA",
            component="Primary Market Data",
            service="/api/quote",
            error_code="PIP-MARKET-QUOTE-002",
            message=f"No usable quote was returned for {original}.",
            symbol=original,
        )
        raise HTTPException(status_code=503, detail=f"No usable quote returned for {original}.")

    diagnostics.resolve(error_code="PIP-MARKET-MASTER-003", symbol=original)
    diagnostics.resolve(error_code="PIP-MARKET-QUOTE-002", symbol=original)
    return to_quote(replace(result, symbol=original))


@app.get("/api/quotes", response_model=list[Quote])
def quotes(
    symbols: str = Query(..., min_length=1, max_length=30000),
    account_id: int | None = Query(default=None, ge=1),
) -> list[Quote]:
    requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    source = configured_source()
    try:
        results = market_data_master.get_quotes(requested)
    except Exception as exc:
        logger.exception(
            "Primary market-data quotes failed: source=%s symbols=%s",
            source, requested[:20],
        )
        diagnostics.record(
            severity="ERROR",
            category="MARKET_DATA",
            component="Primary Market Data",
            service="/api/quotes",
            error_code="PIP-MARKET-MASTER-004",
            message=f"{source} watchlist quotes failed.",
            technical_detail=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail=f"{source} market data quotes failed: {exc}",
        ) from exc
    diagnostics.resolve(error_code="PIP-MARKET-MASTER-004")
    return results


@app.post("/api/quotes", response_model=list[Quote])
def quotes_post(request: QuotesRequest) -> list[Quote]:
    requested = [item.strip().upper() for item in request.symbols if item.strip()]
    try:
        return market_data_master.get_quotes(requested)
    except Exception as exc:
        logger.exception("Primary market-data POST quotes failed: source=%s symbols=%s", configured_source(), requested[:20])
        raise HTTPException(status_code=503, detail=f"{configured_source()} market data quotes failed: {exc}") from exc


def get_quotes_for_symbols(requested: list[str], selected_provider=None) -> list[Quote]:
    """Fetch watchlist quotes through the selected connected broker account.

    The PIPSGOX watchlist stores only the ticker, such as LOTUSDEV. Some NSE
    instruments are available to FYERS in BE instead of EQ, so asking only for
    NSE:<ticker>-EQ can silently produce no quote.
    """
    if selected_provider is None:
        raise HTTPException(status_code=409, detail="Select a connected broker account before loading market data.")
    if not requested:
        return []

    if hasattr(selected_provider, "resolve_instrument") and hasattr(selected_provider, "get_ltp"):
        instruments: list[tuple[str, str, int]] = []
        for clean in requested:
            try:
                resolved = selected_provider.resolve_instrument(clean)
                if resolved and resolved.get("security_id"):
                    instruments.append((clean, str(resolved["exchange_segment"]), int(resolved["security_id"])))
            except (TypeError, ValueError):
                continue

        grouped: dict[str, list[int]] = {}
        for _, segment, security_id in instruments:
            grouped.setdefault(segment, []).append(security_id)

        payload = selected_provider.get_ltp(grouped)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        output: list[Quote] = []
        for clean, segment, security_id in instruments:
            segment_data = data.get(segment, {}) if isinstance(data, dict) else {}
            item = segment_data.get(str(security_id), {}) if isinstance(segment_data, dict) else {}
            if not isinstance(item, dict) or item.get("last_price") is None:
                continue
            output.append(Quote(
                symbol=clean,
                exchange="NSE",
                last=float(item["last_price"]),
                change=0.0,
                change_percent=0.0,
                source="DhanHQ API V2",
            ))
        return output
    if len(requested) > 1000:
        raise HTTPException(
            status_code=400,
            detail="A maximum of 1000 symbols can be requested at once.",
        )

    original_candidates: dict[str, list[str]] = {}
    all_candidates: list[str] = []

    for original in requested:
        clean = original.strip().upper()
        if not clean:
            continue

        candidates = _master_symbol_candidates(clean)
        if not candidates:
            api_symbol = resolve_api_symbol(clean)
            candidates = [api_symbol] if api_symbol else []

        # Only request the supported NSE equity series for ordinary equities.
        # Keep index symbols intact.
        filtered = [
            candidate
            for candidate in candidates
            if candidate.rsplit("-", 1)[-1].upper() in {"EQ", "BE", "INDEX"}
        ]
        if filtered:
            candidates = list(dict.fromkeys(filtered))

        if candidates:
            original_candidates[clean] = candidates
            all_candidates.extend(candidates)

    all_candidates = list(dict.fromkeys(all_candidates))
    quote_by_api: dict[str, Quote] = {}

    # FYERS Quotes supports batches of up to 50 symbols.
    for start in range(0, len(all_candidates), 50):
        chunk = all_candidates[start:start + 50]
        if not chunk:
            continue

        try:
            batch_results = selected_provider.get_quotes(chunk)
        except ValueError:
            # A single invalid/restricted symbol must not hide valid symbols
            # in the same batch.
            batch_results = []
            for candidate in chunk:
                try:
                    batch_results.extend(selected_provider.get_quotes([candidate]))
                except ValueError:
                    continue

        for item in batch_results:
            quote_by_api[item.symbol.upper()] = item

    output: list[Quote] = []
    for original in requested:
        clean = original.strip().upper()
        if not clean:
            continue

        # _master_symbol_candidates() is ordered EQ -> BE. The first returned
        # candidate is therefore the preferred working series.
        for candidate in original_candidates.get(clean, []):
            item = quote_by_api.get(candidate.upper())
            if item is None:
                continue

            # Keep the user's symbol-only watchlist representation in the UI.
            output.append(to_quote(replace(item, symbol=clean)))
            break

    return output
