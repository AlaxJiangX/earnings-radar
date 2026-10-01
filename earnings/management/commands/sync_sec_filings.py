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
    select_monitoring_pool,
)
from earnings.services.sec_filing_sync import (
    SecFilingMatchingSummary,
    execute_sec_filing_sync,
    replay_filing_earnings_matching,
)
from filings.sync import SEC_JOB_TYPE
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
        parser.add_argument("--match-only", action="store_true")
        parser.add_argument("--sync-run", type=UUID)

    def handle(self, *args: object, **options: object) -> None:
        del args
        source_key = cast(str, options["source_key"])
        try:
            source = DataSource.objects.get(key=source_key)
        except DataSource.DoesNotExist:
            raise CommandError("SEC DataSource does not exist.") from None
        try:
            match_only = cast(bool, options["match_only"])
            sync_run_id = cast(UUID | None, options["sync_run"])
            if match_only:
                replay_run_id = self._replay_scope(options, sync_run_id=sync_run_id)
                replay = replay_filing_earnings_matching(
                    source=source,
                    sync_run_id=replay_run_id,
                )
                self.stdout.write(
                    self._matching_line(replay.sync_run, replay.matching, replay=True)
                )
                if replay.matching.matching_failures:
                    raise CommandError("SEC filing matching replay ended with failures.")
                return
            if sync_run_id is not None:
                raise CommandError("--sync-run requires --match-only.")
            provider = SecEdgarProvider(
                user_agent=settings.SEC_USER_AGENT,
                max_requests_per_second=settings.SEC_MAX_REQUESTS_PER_SECOND,
            )
            snapshot = self._resolve_snapshot(options)
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
            orchestration = execute_sec_filing_sync(
                source=source,
                provider=provider,
                snapshot=snapshot,
                idempotency_key=key,
            )
        except (ValueError, RuntimeError) as error:
            raise CommandError(str(error)) from None
        run = orchestration.sec_sync_result.sync_run
        self.stdout.write(
            f"SEC run {run.pk}: {run.status}; fetched={run.fetched_count}; "
            f"created={run.created_count}; skipped={run.skipped_count}; failed={run.failed_count}"
        )
        self.stdout.write(self._matching_line(run, orchestration.matching, replay=False))
        if run.status != SyncRun.Status.SUCCEEDED:
            raise CommandError(f"SEC synchronization ended {run.status}.")

    @staticmethod
    def _matching_line(
        run: SyncRun,
        summary: SecFilingMatchingSummary,
        *,
        replay: bool,
    ) -> str:
        prefix = f"filing matching replay run={run.pk}" if replay else "matching"
        return (
            f"{prefix} evaluated={summary.filings_evaluated}; "
            f"release={summary.matched_release}; periodic={summary.matched_periodic}; "
            f"review={summary.review_required}; no_match={summary.no_match}; "
            f"manual={summary.manual_authority}; failures={summary.matching_failures}"
        )

    @staticmethod
    def _replay_scope(
        options: dict[str, object],
        *,
        sync_run_id: UUID | None,
    ) -> UUID:
        if sync_run_id is None:
            raise CommandError("--match-only requires --sync-run.")
        if (
            options["snapshot_id"] is not None
            or options["retry_run"] is not None
            or options["as_of"] is not None
            or options["idempotency_key"] is not None
        ):
            raise CommandError("--match-only accepts only --source-key and --sync-run.")
        return sync_run_id

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
