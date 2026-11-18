"""Send email, with Resend (default) or Gmail SMTP. MAIL_PROVIDER in .env picks one.

Resend: one HTTPS request to https://api.resend.com/emails with the API key. A free account
sends from onboarding@resend.dev and only to the address you signed up with.

Gmail: the normal route is port 587: connect -> EHLO -> STARTTLS (encrypt) -> EHLO again -> login ->
send. If port 587 cannot even be connected to (some networks block it), we try port 465,
where the connection is encrypted from the start (SMTP_SSL): connect -> EHLO -> login -> send.

Gmail needs an App password (Google Account > Security > 2-Step Verification > App
passwords), not your normal password. The password is never printed: errors are re-raised as
MailError with the password (or API key) removed from the message.
"""

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

import httpx

from pipeline.config import Settings

RESEND_URL = "https://api.resend.com/emails"
RESEND_FROM = "Scholarship Hunter <onboarding@resend.dev>"
RESEND_403_HINT = "the free Resend account can only send to the email you signed up with"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587  # STARTTLS
SMTP_SSL_PORT = 465  # encrypted from the first byte (fallback)
SMTP_TIMEOUT_S = 30.0

SmtpFactory = Callable[[str, int, float], smtplib.SMTP]
Log = Callable[[str], None]


class MailError(Exception):
    """Sending failed. The message never contains the password or API key."""


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str, log: "Log" = ...) -> str: ...


def build_mailer(settings: Settings) -> Mailer:
    """The mailer MAIL_PROVIDER asks for. Raises ValueError if its secret is missing."""
    if settings.mail_provider == "gmail":
        return GmailMailer(settings.gmail_address, settings.gmail_app_password or "")
    return ResendMailer(settings.resend_api_key or "")


def _connect(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


def _connect_ssl(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())


def _quiet(line: str) -> None:
    """The default log: print nothing."""


def build_message(sender: str, to: str, subject: str, text: str, html: str) -> EmailMessage:
    """An email with a plain-text part and an HTML part (the reader's app picks one)."""
    message = EmailMessage()
    message["From"] = f"Scholarship Hunter <{sender}>"
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


class GmailMailer:
    def __init__(
        self,
        address: str,
        app_password: str,
        connect: SmtpFactory = _connect,
        connect_ssl: SmtpFactory = _connect_ssl,
    ) -> None:
        if not address or not app_password:
            raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must both be set in .env")
        self.address = address
        # Google shows app passwords in groups of four ("abcd efgh ..."); spaces are not part.
        self._password = "".join(app_password.split())
        self._connect = connect
        self._connect_ssl = connect_ssl

    def send(self, to: str, subject: str, text: str, html: str, log: Log = _quiet) -> str:
        """Send one email. Returns its Message-ID. Raises MailError if it was not sent.

        `log` is called with one line before each step, so a caller can show progress.
        """
        message = build_message(self.address, to, subject, text, html)
        step = "connect"
        try:
            smtp, encrypted = self._open(log)
            with smtp:  # leaving the block sends QUIT
                step = "EHLO"
                log("EHLO")
                smtp.ehlo()
                if not encrypted:
                    step = "STARTTLS"
                    log("STARTTLS")
                    smtp.starttls(context=ssl.create_default_context())
                    step = "EHLO again"
                    log("EHLO again (now encrypted)")
                    smtp.ehlo()
                step = "login"
                log(f"login as {self.address}")
                smtp.login(self.address, self._password)
                step = "send"
                log(f"send to {to}")
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # "from None": the original error (and its traceback) is not attached.
            raise MailError(f"Could not send email to {to} (failed at {step}): "
                            f"{self._safe(exc)}") from None  # fmt: skip
        log("done")
        return str(message["Message-ID"])

    def _open(self, log: Log) -> tuple[smtplib.SMTP, bool]:
        """Connect on port 587; if that fails, on port 465 with SSL. Returns (smtp, encrypted)."""
        log(f"connect to {SMTP_HOST}:{SMTP_PORT} (timeout {SMTP_TIMEOUT_S:.0f} s)")
        try:
            return self._connect(SMTP_HOST, SMTP_PORT, SMTP_TIMEOUT_S), False
        except OSError as exc:  # smtplib errors are OSErrors too
            log(f"port {SMTP_PORT} failed ({self._safe(exc)})")
        log(f"connect to {SMTP_HOST}:{SMTP_SSL_PORT} with SSL (timeout {SMTP_TIMEOUT_S:.0f} s)")
        return self._connect_ssl(SMTP_HOST, SMTP_SSL_PORT, SMTP_TIMEOUT_S), True

    def _safe(self, exc: BaseException) -> str:
        """The error's type and message, with the password hidden."""
        return f"{type(exc).__name__}: {str(exc).replace(self._password, '***')}"


class ResendMailer:
    """Sends through the Resend HTTP API. Never retries, so an email is never sent twice."""

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key.strip():
            raise ValueError("RESEND_API_KEY must be set in .env (or use MAIL_PROVIDER=gmail)")
        self._key = api_key.strip()
        self._client = client or httpx.Client(timeout=SMTP_TIMEOUT_S)

    def send(self, to: str, subject: str, text: str, html: str, log: Log = _quiet) -> str:
        """Send one email. Returns Resend's email id. Raises MailError if it was not sent."""
        log(f"POST {RESEND_URL} (to {to}, from {RESEND_FROM})")
        payload = {"from": RESEND_FROM, "to": [to], "subject": subject, "html": html, "text": text}
        try:
            response = self._client.post(
                RESEND_URL, json=payload, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.HTTPError as exc:
            raise MailError(f"Could not send email to {to}: {self._safe(exc)}") from None
        log(f"Resend answered HTTP {response.status_code}")
        if response.is_success:
            log("done")
            return str(response.json().get("id", ""))
        reason = _resend_message(response).replace(self._key, "***")
        if response.status_code == 403:
            reason = f"{reason} ({RESEND_403_HINT})"
        raise MailError(f"Could not send email to {to}: HTTP {response.status_code}: {reason}")

    def _safe(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {str(exc).replace(self._key, '***')}"


def _resend_message(response: httpx.Response) -> str:
    """Resend's error text: the "message" field of its JSON answer, else the raw body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or "no details"
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return response.text[:300] or "no details"
