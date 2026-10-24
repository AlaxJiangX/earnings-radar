"""Enqueue due advance (one-day-before) reminders for the market date.

Stage 6.2W: the production entry point for
``notifications.services.dispatch.enqueue_due_advance_reminders``.  The
market date comes from ``earnings.presentation.business_date()``
(ADR-024 America/New_York anchor); override it only for replays.

``--dry-run`` runs the whole scan inside a transaction that is always
rolled back, so it reports exactly what a real run would enqueue without
writing anything.  Cron registration is a production-deployment concern
and is intentionally not configured here.
"""

from __future__ import annotations

from datetime import date
from typing import cast

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from earnings.presentation import business_date
from notifications.services.dispatch import enqueue_due_advance_reminders


class Command(BaseCommand):
    help = (
        "Enqueue due advance reminders (earnings_upcoming) for the current "
        "market date; reruns are idempotent."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--as-of",
            type=date.fromisoformat,
            default=None,
            help="Override the market date (ISO format) for replays.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be enqueued without writing anything.",
        )

    def handle(self, *args: object, **options: object) -> None:
        del args
        market_date = cast("date | None", options["as_of"]) or business_date()
        dry_run = cast("bool", options["dry_run"])

        if dry_run:
            with transaction.atomic():
                outcome = enqueue_due_advance_reminders(market_date=market_date)
                transaction.set_rollback(True)
        else:
            outcome = enqueue_due_advance_reminders(market_date=market_date)

        created = outcome.created
        reactivated = outcome.reactivated
        total = len(outcome.results)
        self.stdout.write(
            f"market_date={market_date.isoformat()} dry_run={str(dry_run).lower()} "
            f"enqueued={created} reactivated={reactivated} total_decisions={total}"
        )
