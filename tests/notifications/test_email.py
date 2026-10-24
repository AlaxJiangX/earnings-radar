"""Email adapter classification tests (ADR-027 D1/D3).

Pure unit tests over synthetic ``smtplib`` exceptions — no network, no
database.  The classification table is the decided product contract:
permanent only for invalid recipients, spam/content rejection and
configuration-level errors; everything else temporary or unknown.
"""

from __future__ import annotations

import smtplib
from typing import Any
from unittest.mock import patch

import notifications.services.email as email_module
from notifications.services.email import (
    DeliveryOutcome,
    EmailRequest,
    EmailSendResult,
    FakeEmailSender,
    SmtpEmailSender,
    _classify,
    validate_email_sender_configured,
)


def _recipients_refused(code: int) -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({"user@example.test": (code, b"refused")})


class TestClassification:
    def test_recipient_refused_5xx_is_permanent_invalid_recipient(self) -> None:
        result = _classify(_recipients_refused(550))
        assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
        assert result.error_code == "invalid_recipient"

    def test_recipient_refused_4xx_is_temporary(self) -> None:
        result = _classify(_recipients_refused(452))
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "smtp_recipients_refused"

    def test_data_error_571_is_permanent_spam_rejected(self) -> None:
        result = _classify(smtplib.SMTPDataError(550, "5.7.1 Message content rejected"))
        assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
        assert result.error_code == "spam_rejected"

    def test_data_error_511_is_permanent_invalid_recipient(self) -> None:
        result = _classify(
            smtplib.SMTPDataError(550, "5.1.1 <user@example.test>: Recipient unknown")
        )
        assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
        assert result.error_code == "invalid_recipient"

    def test_data_error_other_5xx_is_temporary(self) -> None:
        result = _classify(smtplib.SMTPDataError(554, "Transaction failed unexpectedly"))
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "smtp_data_error"

    def test_connect_error_is_temporary_network(self) -> None:
        result = _classify(smtplib.SMTPConnectError(421, "Service not available"))
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "network_error"

    def test_oserror_is_temporary_network(self) -> None:
        result = _classify(OSError("connection reset"))
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "network_error"

    def test_authentication_error_is_permanent(self) -> None:
        result = _classify(
            smtplib.SMTPAuthenticationError(535, "Authentication credentials invalid")
        )
        assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
        assert result.error_code == "smtp_auth_error"

    def test_generic_smtp_exception_is_temporary(self) -> None:
        result = _classify(smtplib.SMTPException("421 too many connections"))
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "smtp_error"

    def test_generic_exception_is_unknown(self) -> None:
        result = _classify(RuntimeError("provider SDK exploded"))
        assert result.outcome is DeliveryOutcome.UNKNOWN
        assert result.error_code == "unclassified"

    def test_error_messages_are_sanitized_and_bounded(self) -> None:
        result = _classify(RuntimeError("line1\nline2   spaces   " + "x" * 500))
        assert result.error_message is not None
        assert "\n" not in result.error_message
        assert len(result.error_message) <= 300


class _StubMessage:
    """Stands in for ``EmailMessage`` inside ``SmtpEmailSender.send``."""

    def __init__(self, *, outcome: str) -> None:
        self._outcome = outcome

    def send(self) -> int:
        if self._outcome == "raise":
            raise smtplib.SMTPConnectError(421, "Service not available")
        if self._outcome == "zero":
            return 0
        return 1


class TestSmtpSender:
    def _send(self, outcome: str) -> EmailSendResult:
        sender = SmtpEmailSender()
        factory = lambda **kwargs: _StubMessage(outcome=outcome)  # noqa: E731
        with patch.object(email_module, "EmailMessage", factory):
            return sender.send(EmailRequest(recipient="user@example.test", subject="s", body="b"))

    def test_accepted(self) -> None:
        result = self._send("ok")
        assert result.outcome is DeliveryOutcome.ACCEPTED

    def test_exception_is_classified(self) -> None:
        result = self._send("raise")
        assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert result.error_code == "network_error"

    def test_zero_send_count_is_unknown(self) -> None:
        result = self._send("zero")
        assert result.outcome is DeliveryOutcome.UNKNOWN
        assert result.error_code == "send_returned_zero"


class TestFakeSender:
    def test_records_and_accepts_by_default(self) -> None:
        fake = FakeEmailSender()
        request = EmailRequest(recipient="user@example.test", subject="s", body="b")

        result = fake.send(request)

        assert result.outcome is DeliveryOutcome.ACCEPTED
        assert fake.sent == [request]

    def test_scripted_results_replay_in_order(self) -> None:
        temporary = EmailSendResult(
            outcome=DeliveryOutcome.TEMPORARY_FAILURE, error_code="network_error"
        )
        fake = FakeEmailSender(scripted=(temporary,))
        request = EmailRequest(recipient="user@example.test", subject="s", body="b")

        first = fake.send(request)
        second = fake.send(request)

        assert first.outcome is DeliveryOutcome.TEMPORARY_FAILURE
        assert second.outcome is DeliveryOutcome.ACCEPTED
        assert len(fake.sent) == 2


class TestConfigValidation:
    def test_missing_host_is_rejected(self, settings: Any) -> None:
        settings.EMAIL_HOST = ""
        settings.DEFAULT_FROM_EMAIL = "radar@example.test"

        try:
            validate_email_sender_configured(SmtpEmailSender())
        except email_module.EmailDeliveryConfigError as error:
            assert "EMAIL_HOST" in str(error)
        else:
            raise AssertionError("expected configuration error")

    def test_missing_from_address_is_rejected(self, settings: Any) -> None:
        settings.EMAIL_HOST = "smtp.example.test"
        settings.DEFAULT_FROM_EMAIL = ""

        try:
            validate_email_sender_configured(SmtpEmailSender())
        except email_module.EmailDeliveryConfigError as error:
            assert "DEFAULT_FROM_EMAIL" in str(error)
        else:
            raise AssertionError("expected configuration error")

    def test_fake_sender_needs_no_configuration(self) -> None:
        validate_email_sender_configured(FakeEmailSender())
