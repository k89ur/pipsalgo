from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryUnavailable(RuntimeError):
    """Raised when no server-side email delivery provider is configured."""


class EmailDeliveryError(RuntimeError):
    """Raised when the configured email provider cannot deliver the message."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EmailDeliveryUnavailable("Email delivery is not configured.")
    return value


def _send_resend(
    *,
    to_email: str,
    from_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    api_key = _required("PIPSGOX_RESEND_API_KEY")

    try:
        response = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "text": text_body,
                "html": html_body,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise EmailDeliveryError("Email provider could not be reached.") from exc

    if not response.ok:
        # Keep provider response details out of the browser. They may contain
        # implementation-specific information that is useful only in server logs.
        raise EmailDeliveryError(
            f"Email provider rejected the message (HTTP {response.status_code})."
        )


def _send_smtp(
    *,
    to_email: str,
    from_email: str,
    subject: str,
    text_body: str,
) -> None:
    host = _required("PIPSGOX_SMTP_HOST")
    username = os.getenv("PIPSGOX_SMTP_USERNAME", "").strip()
    password = os.getenv("PIPSGOX_SMTP_PASSWORD", "")
    port = int(os.getenv("PIPSGOX_SMTP_PORT", "587"))
    security = os.getenv("PIPSGOX_SMTP_SECURITY", "starttls").strip().lower()

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)

    context = ssl.create_default_context()

    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        elif security == "none":
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("SMTP email delivery failed.") from exc


def _send(
    *,
    to_email: str,
    from_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    provider = os.getenv("PIPSGOX_EMAIL_PROVIDER", "auto").strip().lower()

    if provider not in {"auto", "resend", "smtp"}:
        raise EmailDeliveryUnavailable("Email delivery provider is not configured correctly.")

    # Resend is the preferred server-side transactional provider. SMTP remains
    # available as a generic fallback for self-hosted deployments.
    if provider == "resend" or (
        provider == "auto" and os.getenv("PIPSGOX_RESEND_API_KEY", "").strip()
    ):
        _send_resend(
            to_email=to_email,
            from_email=from_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        return

    if provider == "smtp" or (
        provider == "auto" and os.getenv("PIPSGOX_SMTP_HOST", "").strip()
    ):
        _send_smtp(
            to_email=to_email,
            from_email=from_email,
            subject=subject,
            text_body=text_body,
        )
        return

    raise EmailDeliveryUnavailable("Email delivery is not configured.")


def send_password_reset_email(*, email: str, token: str) -> None:
    web_url = os.getenv("PIPSGOX_WEB_URL", "http://localhost:3001").rstrip("/")
    # Keep the reset token in the URL fragment so browsers do not send it in
    # HTTP requests, access logs, or Referer headers.
    link = f"{web_url}/#reset_token={quote(token, safe='')}"
    from_email = _required("PIPSGOX_EMAIL_FROM")
    safe_link = html.escape(link, quote=True)

    text_body = (
        "Reset your PIPSGOX password by opening this link:\n\n"
        f"{link}\n\n"
        "This link expires in 30 minutes and can only be used once.\n"
        "After resetting your password, you will need to sign in again.\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = (
        "<!doctype html><html><body>"
        "<h2>Reset your PIPSGOX password</h2>"
        "<p>Click the button below to choose a new password.</p>"
        f'<p><a href="{safe_link}" '
        'style="display:inline-block;padding:12px 18px;background:#2f6f9f;color:#fff;'
        'text-decoration:none;border-radius:6px;font-weight:600;">Reset password</a></p>'
        "<p>This link expires in 30 minutes and can only be used once.</p>"
        "<p>After resetting your password, you will need to sign in again.</p>"
        "<p>If you did not request this, you can ignore this email.</p>"
        "</body></html>"
    )

    _send(
        to_email=email,
        from_email=from_email,
        subject="Reset your PIPSGOX password",
        text_body=text_body,
        html_body=html_body,
    )


def send_verification_email(*, email: str, token: str) -> None:
    web_url = os.getenv("PIPSGOX_WEB_URL", "http://localhost:3001").rstrip("/")
    # Keep the one-time verification token in the URL fragment. Browsers do not
    # send fragments in HTTP requests, so the token is not exposed to server
    # access logs, reverse proxies, or Referer headers. The frontend extracts
    # it and submits it to the verification endpoint in a POST body.
    link = f"{web_url}/#email_verify_token={quote(token, safe='')}"
    from_email = _required("PIPSGOX_EMAIL_FROM")
    safe_link = html.escape(link, quote=True)

    text_body = (
        "Verify your PIPSGOX email address by opening this link:\n\n"
        f"{link}\n\n"
        "This link expires in 24 hours and can only be used once.\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = (
        "<!doctype html><html><body>"
        "<h2>Verify your PIPSGOX email address</h2>"
        "<p>Click the button below to verify your email address.</p>"
        f'<p><a href="{safe_link}" '
        'style="display:inline-block;padding:12px 18px;background:#2f6f9f;color:#fff;'
        'text-decoration:none;border-radius:6px;font-weight:600;">Verify email</a></p>'
        "<p>This link expires in 24 hours and can only be used once.</p>"
        "<p>If you did not request this, you can ignore this email.</p>"
        "</body></html>"
    )

    _send(
        to_email=email,
        from_email=from_email,
        subject="Verify your PIPSGOX email address",
        text_body=text_body,
        html_body=html_body,
    )

def send_totp_disable_confirmation_email(*, email: str, token: str) -> None:
    web_url = os.getenv("PIPSGOX_WEB_URL", "http://localhost:3001").rstrip("/")
    # Keep the one-time token in the URL fragment. The browser removes the
    # fragment before making the initial request, and the frontend submits
    # the token to the confirmation endpoint in a POST body.
    link = f"{web_url}/#totp_disable_token={quote(token, safe='')}"
    from_email = _required("PIPSGOX_EMAIL_FROM")
    safe_link = html.escape(link, quote=True)

    text_body = (
        "A request was made to disable the PIPSGOX authenticator app.

"
        f"Confirm the request by opening this link:
{link}

"
        "This confirmation expires in 15 minutes and can only be used once.
"
        "If you did not request this, ignore this email and keep your authenticator enabled."
    )
    html_body = (
        "<!doctype html><html><body>"
        "<h2>Confirm disabling your PIPSGOX authenticator</h2>"
        "<p>A request was made to disable the authenticator app on your account.</p>"
        f'<p><a href="{safe_link}" '
        'style="display:inline-block;padding:12px 18px;background:#b42318;color:#fff;'
        'text-decoration:none;border-radius:6px;font-weight:600;">CONFIRM DISABLE</a></p>'
        "<p>This confirmation expires in 15 minutes and can only be used once.</p>"
        "<p>If you did not request this, ignore this email and keep your authenticator enabled.</p>"
        "</body></html>"
    )

    _send(
        to_email=email,
        from_email=from_email,
        subject="Confirm disabling your PIPSGOX authenticator",
        text_body=text_body,
        html_body=html_body,
    )
\n