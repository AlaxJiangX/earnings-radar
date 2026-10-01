"""SEC filing synchronization with Filing ↔ Earnings matching orchestration."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from audit.models import DataSource, DomainTargetType, SourceEvidence, SyncRun
from earnings.models import MonitoringPoolSnapshot
from earnings.services.filing_links import (
    FilingEarningsEvaluationResult,
    evaluate_filing_earnings_link,
)
from earnings.services.monitoring_pool import resolve_monitoring_pool_snapshot_contract
from filings.models import Filing
from filings.sync import SEC_JOB_TYPE, SecSyncResult, sync_sec_filings
from providers.base import Provider

_MATCHING_OUTCOMES = frozenset(
    {
        "matched_release_filing",
        "matched_periodic_filing",
        "review_required",
        "no_match",
        "manual_authority",
    }
)


class SecFilingMatchingError(RuntimeError):
    """A persisted Filing could not be safely evaluated for earnings matching."""


class SecFilingReplayError(RuntimeError):
    """A persisted-only matching replay request is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class SecFilingMatchingSummary:
    """In-memory outcome counts for one orchestration invocation."""

    filings_evaluated: int
    matched_release: int
    matched_periodic: int
    review_required: int
    no_match: int
    manual_authority: int
    matching_failures: int


@dataclass(frozen=True, slots=True)
class SecFilingOrchestrationResult:
    sec_sync_result: SecSyncResult
    matching: SecFilingMatchingSummary


@dataclass(frozen=True, slots=True)
class SecFilingReplayResult:
    sync_run: SyncRun
    matching: SecFilingMatchingSummary


def execute_sec_filing_sync(
    *,
    source: DataSource,
    provider: Provider,
    snapshot: MonitoringPoolSnapshot,
    idempotency_key: str,
) -> SecFilingOrchestrationResult:
    """Run one SEC sync and match every Filing persisted by that same run."""

    pool = resolve_monitoring_pool_snapshot_contract(
        as_of=snapshot.as_of_date,
        selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
    )
    company_ids = tuple(member.company_id for member in pool.members)
    outcomes: list[FilingEarningsEvaluationResult] = []
    matching_failures = 0

    def evaluate(filing: Filing, sync_run: SyncRun) -> None:
        nonlocal matching_failures
        try:
            outcome = evaluate_filing_earnings_link(filing=filing, sync_run=sync_run)
            if outcome.outcome not in _MATCHING_OUTCOMES:
                raise SecFilingMatchingError(
                    f"Unknown Filing earnings outcome {outcome.outcome!r}."
                )
        except Exception as error:
            matching_failures += 1
            raise SecFilingMatchingError("Filing earnings matching failed.") from error
        outcomes.append(outcome)

    sec_result = sync_sec_filings(
        source=source,
        provider=provider,
        company_ids=company_ids,
        pool_as_of=pool.snapshot.as_of_date,
        pool_selector_version=pool.snapshot.selector_version,
        pool_hash=pool.snapshot.pool_hash,
        idempotency_key=idempotency_key,
        on_filing_persisted=evaluate,
    )
    return SecFilingOrchestrationResult(
        sec_sync_result=sec_result,
        matching=_summarize(outcomes, matching_failures=matching_failures),
    )


def replay_filing_earnings_matching(
    *,
    source: DataSource,
    sync_run_id: UUID,
) -> SecFilingReplayResult:
    """Re-run matching over one persisted SEC SyncRun without any network access."""

    if source._state.adding or source.pk is None:
        raise SecFilingReplayError("SEC DataSource must be persisted.")
    current_source = DataSource.objects.get(pk=source.pk)
    try:
        run = SyncRun.objects.get(pk=sync_run_id)
    except SyncRun.DoesNotExist:
        raise SecFilingReplayError("SyncRun does not exist.") from None
    if run.job_type != SEC_JOB_TYPE:
        raise SecFilingReplayError("SyncRun is not a SEC filing run.")
    if run.source_id != current_source.pk:
        raise SecFilingReplayError("SyncRun belongs to a different SEC DataSource.")

    filing_ids = list(
        SourceEvidence.objects.filter(
            target_type=DomainTargetType.FILING,
            sync_run_id=run.pk,
        )
        .order_by("target_id")
        .values_list("target_id", flat=True)
        .distinct()
    )
    filings = list(Filing.objects.filter(pk__in=filing_ids).order_by("accepted_at", "id"))
    if len(filings) != len(filing_ids):
        raise SecFilingReplayError("SEC replay provenance references missing Filing targets.")

    outcomes: list[FilingEarningsEvaluationResult] = []
    matching_failures = 0
    for filing in filings:
        try:
            outcome = evaluate_filing_earnings_link(filing=filing, sync_run=run)
            if outcome.outcome not in _MATCHING_OUTCOMES:
                raise SecFilingMatchingError(
                    f"Unknown Filing earnings outcome {outcome.outcome!r}."
                )
        except Exception:
            matching_failures += 1
            continue
        outcomes.append(outcome)
    return SecFilingReplayResult(
        sync_run=run,
        matching=_summarize(outcomes, matching_failures=matching_failures),
    )


def _summarize(
    outcomes: Sequence[FilingEarningsEvaluationResult],
    *,
    matching_failures: int,
) -> SecFilingMatchingSummary:
    counts = Counter(item.outcome for item in outcomes)
    unknown = set(counts) - _MATCHING_OUTCOMES
    if unknown:
        raise SecFilingMatchingError(
            f"Unknown Filing earnings outcomes: {', '.join(sorted(unknown))}."
        )
    return SecFilingMatchingSummary(
        filings_evaluated=len(outcomes),
        matched_release=counts.get("matched_release_filing", 0),
        matched_periodic=counts.get("matched_periodic_filing", 0),
        review_required=counts.get("review_required", 0),
        no_match=counts.get("no_match", 0),
        manual_authority=counts.get("manual_authority", 0),
        matching_failures=matching_failures,
    )
