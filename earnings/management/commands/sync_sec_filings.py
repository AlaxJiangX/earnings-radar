"""Run one bounded SEC metadata sync against a persisted monitoring pool."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from audit.models import DataSource, SyncRun
from earnings.models import MonitoringPoolSnapshot
from earnings.services.monitoring_pool import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    resolve_monitoring_pool_snapshot_contract,
    select_monitoring_pool,
)
from filings.sync import SEC_JOB_TYPE, sync_sec_filings
from indexes.models import MarketIndex
from providers.sec_edgar import SecEdgarProvider


class Command(BaseCommand):
    help = "Synchronize SEC filing metadata for one frozen monitoring pool."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--source-key", required=True)
        parser.add_argument("--as-of", type=date.fromisoformat)
        parser.add_argument("--snapshot-id", type=UUID)
        parser.add_argument("--retry-run", type=UUID)
        parser.add_argument("--idempotency-key")

    def handle(self, *args: object, **options: object) -> None:
        del args
        source_key = cast(str, options["source_key"])
        try:
            source = DataSource.objects.get(key=source_key)
        except DataSource.DoesNotExist:
            raise CommandError("SEC DataSource does not exist.") from None
        try:
            provider = SecEdgarProvider(
                user_agent=settings.SEC_USER_AGENT,
                max_requests_per_second=settings.SEC_MAX_REQUESTS_PER_SECOND,
            )
            snapshot = self._resolve_snapshot(options)
            pool = resolve_monitoring_pool_snapshot_contract(
                as_of=snapshot.as_of_date,
                selector_version=snapshot.selector_version,
                pool_hash=snapshot.pool_hash,
            )
            company_ids = tuple(member.company_id for member in pool.members)
            now = timezone.now().astimezone(UTC)
            bucket = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
            retry_id = cast(UUID | None, options["retry_run"])
            if retry_id is not None and SyncRun.objects.get(pk=retry_id).source_id != source.pk:
                raise CommandError("Retry run belongs to a different SEC DataSource.")
            default_key = (
                f"sec-v1:{snapshot.pool_hash}:{bucket.isoformat()}"
                if retry_id is None
                else f"sec-retry-v1:{retry_id}:{bucket.isoformat()}"
            )
            key = cast(str | None, options["idempotency_key"]) or default_key
            result = sync_sec_filings(
                source=source,
                provider=provider,
                company_ids=company_ids,
                pool_as_of=snapshot.as_of_date,
                pool_selector_version=snapshot.selector_version,
                pool_hash=snapshot.pool_hash,
                idempotency_key=key,
            )
        except (ValueError, RuntimeError) as error:
            raise CommandError(str(error)) from None
        run = result.sync_run
        self.stdout.write(
            f"SEC run {run.pk}: {run.status}; fetched={run.fetched_count}; "
            f"created={run.created_count}; skipped={run.skipped_count}; failed={run.failed_count}"
        )
        if run.status != SyncRun.Status.SUCCEEDED:
            raise CommandError(f"SEC synchronization ended {run.status}.")

    def _resolve_snapshot(self, options: dict[str, object]) -> MonitoringPoolSnapshot:
        retry_id = cast(UUID | None, options["retry_run"])
        snapshot_id = cast(UUID | None, options["snapshot_id"])
        if retry_id is not None and snapshot_id is not None:
            raise CommandError("Use --retry-run or --snapshot-id, not both.")
        if retry_id is not None:
            try:
                run = SyncRun.objects.get(pk=retry_id)
            except SyncRun.DoesNotExist:
                raise CommandError("Retry run does not exist.") from None
            if run.job_type != SEC_JOB_TYPE or run.status not in {
                SyncRun.Status.FAILED,
                SyncRun.Status.PARTIAL,
            }:
                raise CommandError("Retry source must be a failed or partial SEC run.")
            scope = run.scope
            try:
                return MonitoringPoolSnapshot.objects.get(
                    as_of_date=date.fromisoformat(scope["monitoring_pool_as_of"]),
                    selector_version=scope["selector_version"],
                    pool_hash=scope["monitoring_pool_hash"],
                )
            except (KeyError, TypeError, MonitoringPoolSnapshot.DoesNotExist):
                raise CommandError("Retry run has no valid frozen monitoring pool.") from None
        if snapshot_id is not None:
            try:
                return MonitoringPoolSnapshot.objects.get(pk=snapshot_id)
            except MonitoringPoolSnapshot.DoesNotExist:
                raise CommandError("Monitoring pool snapshot does not exist.") from None
        as_of = cast(date | None, options["as_of"])
        if as_of is None:
            as_of = datetime.now(ZoneInfo("America/New_York")).date()
        enabled_codes = tuple(
            MarketIndex.objects.filter(is_enabled=True)
            .order_by("code")
            .values_list("code", flat=True)
        )
        return select_monitoring_pool(
            as_of=as_of,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=enabled_codes,
        ).snapshot
