"""Explicitly opted-in, one-request live SEC submissions check."""

from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from filings.parsing import parse_submissions
from providers.sec_edgar import SecEdgarProvider, submissions_request


class Command(BaseCommand):
    help = "One live SEC request, with no database writes. Requires --allow-live-sec."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--allow-live-sec", action="store_true")
        parser.add_argument("--cik", required=True)

    def handle(self, *args: object, **options: object) -> None:
        del args
        if not options["allow_live_sec"]:
            raise CommandError("Live SEC access requires --allow-live-sec.")
        try:
            provider = SecEdgarProvider(
                user_agent=settings.SEC_USER_AGENT,
                max_requests_per_second=settings.SEC_MAX_REQUESTS_PER_SECOND,
            )
            cik = str(options["cik"])
            result = provider.fetch(submissions_request(cik=cik, started_at=datetime.now(UTC)))
            filings = parse_submissions(result.raw_content, cik=cik)
        except (ValueError, RuntimeError) as error:
            raise CommandError(str(error)) from None
        self.stdout.write(f"SEC submissions parsed; target filings={len(filings)}")
