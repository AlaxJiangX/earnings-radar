"""Deliver due notifications: claim, re-check, send, record (ADR-027).

Stage 6.4 production entry point for
``notifications.services.delivery.send_pending_notifications``.  Designed
for a 5-minute cron (registration is a production-deployment concern and is
intentionally not configured here).

``--dry-run`` executes the whole pipeline with a fake sender (no network)
inside a transaction that is always rolled back, reporting exactly what a
real run would claim and how rows would close out.

The SMTP sender refuses to start when ``EMAIL_HOST`` /
``DEFAULT_FROM_EMAIL`` are not configured (ADR-027 D1): nothing is claimed
and no attempts are recorded until the operator fixes the environment.
"""

from __future__ import annotations

from typing import cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone as django_timezone

from notifications.services.delivery import send_pending_notifications
from notifications.services.email import (
    EmailDeliveryConfigError,
    FakeEmailSender,
    default_email_sender,
    validate_email_sender_configured,
)


class Command(BaseCommand):
    help = (
        "Deliver due notifications (email + in-app); reruns are idempotent "
        "and concurrent runs never claim the same row."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the whole pipeline with a fake sender and roll everything back.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Rows claimed per batch (default 50).",
        )

    def handle(self, *args: object, **options: object) -> None:
        del args
        dry_run = cast("bool", options["dry_run"])
        batch_size = cast("int", options["batch_size"])
        if batch_size <= 0:
            raise CommandError("--batch-size must be positive.")

        sender = FakeEmailSender() if dry_run else default_email_sender()
        if not dry_run:
            try:
                validate_email_sender_configured(sender)
            except EmailDeliveryConfigError as error:
                raise CommandError(str(error)) from error

        now = django_timezone.now()
        if dry_run:
            with transaction.atomic():
                result = send_pending_notifications(now=now, sender=sender, batch_size=batch_size)
                transaction.set_rollback(True)
        else:
            result = send_pending_notifications(now=now, sender=sender, batch_size=batch_size)

        self.stdout.write(
            f"dry_run={str(dry_run).lower()} claimed={result.claimed} "
            f"sent={result.sent} suppressed={result.suppressed} "
            f"failed={result.failed} scheduled_retry={result.scheduled_retry} "
            f"digest_emails={result.digest_emails}"
        )
