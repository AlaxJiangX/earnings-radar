"""Email delivery adapter: generic SMTP plus a test fake (ADR-027 D1/D3).

The real sender rides on Django's native SMTP backend with zero new
dependencies; every credential comes from ``EMAIL_*`` environment settings.
Failure classification follows the product decision recorded in ADR-027 D3:

- permanent: invalid recipient (RCPT-phase 5xx refusals / data-phase 5.1.x),
  spam or content rejection (5.7.x), authentication and protocol-support
  errors (configuration-level, retrying identical credentials is noise);
- temporary: network/connect/timeout errors, 4xx throttling, and submission
  5xx that is neither an invalid recipient nor a spam rejection;
- unclassifiable exceptions map to ``unknown`` and participate in the same
  bounded retry (the 3-attempt cap backstops them).

Nothing here writes domain tables: the delivery service records attempts and
status transitions; this module only turns one send request into one
classified result.
"""

from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from django.core.mail import EmailMessage

_ERROR_MESSAGE_LIMIT = 300
_ENHANCED_STATUS = re.compile(r"^\s*([245])\.(\d{1,3})\.(\d{1,3})")


class EmailDeliveryConfigError(RuntimeError):
    """Raised when the SMTP sender lacks required environment configuration."""


class DeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EmailRequest:
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    outcome: DeliveryOutcome
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_accepted(self) -> bool:
        return self.outcome is DeliveryOutcome.ACCEPTED

    @property
    def is_permanent_failure(self) -> bool:
        return self.outcome is DeliveryOutcome.PERMANENT_FAILURE


class EmailSender(Protocol):
    def send(self, request: EmailRequest) -> EmailSendResult: ...


def _sanitize(message: object) -> str:
    text = " ".join(str(message).split())
    return text[:_ERROR_MESSAGE_LIMIT]


def _accepted() -> EmailSendResult:
    return EmailSendResult(outcome=DeliveryOutcome.ACCEPTED)


def _temporary(error_code: str, error: Exception | None = None) -> EmailSendResult:
    return EmailSendResult(
        outcome=DeliveryOutcome.TEMPORARY_FAILURE,
        error_code=error_code,
        error_message=_sanitize(error) if error is not None else None,
    )


def _permanent(error_code: str, error: Exception | None = None) -> EmailSendResult:
    return EmailSendResult(
        outcome=DeliveryOutcome.PERMANENT_FAILURE,
        error_code=error_code,
        error_message=_sanitize(error) if error is not None else None,
    )


def _enhanced_status(message: bytes | str) -> str | None:
    text = message.decode("ascii", "replace") if isinstance(message, bytes) else message
    match = _ENHANCED_STATUS.match(text)
    if match is None:
        return None
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def _classify(error: Exception) -> EmailSendResult:
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        # All recipients were refused in the RCPT phase; only an all-5xx
        # refusal proves the addresses invalid (hard bounce class).
        codes = [code for _email, (code, _response) in error.recipients.items()]
        if codes and all(code >= 500 for code in codes):
            return _permanent("invalid_recipient", error)
        return _temporary("smtp_recipients_refused", error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return _permanent("smtp_auth_error", error)
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return _permanent("smtp_not_supported", error)
    if isinstance(error, smtplib.SMTPDataError):
        status = _enhanced_status(error.smtp_error)
        # Only the explicitly decided permanent classes stop immediately
        # (ADR-027 D3); an unparseable 5xx stays retryable.
        if error.smtp_code >= 500 and status is not None and status.startswith("5.7."):
            return _permanent("spam_rejected", error)
        if error.smtp_code >= 500 and status is not None and status.startswith("5.1."):
            return _permanent("invalid_recipient", error)
        return _temporary("smtp_data_error", error)
    if isinstance(error, smtplib.SMTPConnectError | smtplib.SMTPHeloError):
        return _temporary("network_error", error)
    if isinstance(error, smtplib.SMTPException):
        # SMTPException subclasses OSError; generic SMTP failures (sender
        # refusal, submission 5xx, 4xx throttling) are temporary per the
        # decided classification (ADR-027 D3).
        return _temporary("smtp_error", error)
    if isinstance(error, OSError):
        return _temporary("network_error", error)
    return EmailSendResult(
        outcome=DeliveryOutcome.UNKNOWN,
        error_code="unclassified",
        error_message=_sanitize(error),
    )


class SmtpEmailSender:
    """Send through Django's SMTP backend and classify failures (ADR-027 D1)."""

    def send(self, request: EmailRequest) -> EmailSendResult:
        try:
            message = EmailMessage(
                subject=request.subject,
                body=request.body,
                to=[request.recipient],
            )
            sent_count = message.send()
        except Exception as error:  # classified below, never re-raised
            return _classify(error)
        if sent_count != 1:
            return EmailSendResult(
                outcome=DeliveryOutcome.UNKNOWN,
                error_code="send_returned_zero",
                error_message=f"send() returned {sent_count}",
            )
        return _accepted()


class FakeEmailSender:
    """Test double: records every request and replays scripted results.

    With no script, every send is accepted.  Scripted results are consumed
    one per send in order, so retry flows can assert attempt counts.
    """

    def __init__(self, scripted: tuple[EmailSendResult, ...] = ()) -> None:
        self.sent: list[EmailRequest] = []
        self._scripted: list[EmailSendResult] = list(scripted)

    def send(self, request: EmailRequest) -> EmailSendResult:
        self.sent.append(request)
        if self._scripted:
            return self._scripted.pop(0)
        return _accepted()


def default_email_sender() -> EmailSender:
    """Return the configured production sender (generic SMTP, ADR-027 D1)."""

    return SmtpEmailSender()


def validate_email_sender_configured(sender: EmailSender) -> None:
    """Refuse to run delivery with an unconfigured SMTP sender (ADR-027 D1).

    Missing configuration must be loud and happen before any notification is
    claimed: no attempts, no status transitions, nothing to roll back.
    """

    from django.conf import settings as django_settings

    if not isinstance(sender, SmtpEmailSender):
        return
    missing = [
        name
        for name, value in (
            ("EMAIL_HOST", django_settings.EMAIL_HOST),
            ("DEFAULT_FROM_EMAIL", django_settings.DEFAULT_FROM_EMAIL),
        )
        if not value
    ]
    if missing:
        raise EmailDeliveryConfigError(
            f"Email delivery is not configured: missing {', '.join(missing)}."
        )
