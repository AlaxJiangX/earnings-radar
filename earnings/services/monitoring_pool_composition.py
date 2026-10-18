"""Current-state Monitoring-Pool v2 composition and explicit monitoring recompute."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from audit.models import SyncRun
from companies.models import Company
from companies.selectors import company_identity_pending_as_of
from companies.services import (
    CompanyMonitoringStatusResult,
    recalculate_company_monitoring_status,
)
from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot
from earnings.services.monitoring_pool import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
    MonitoringPoolSelectionResult,
    select_monitoring_pool,
)
from indexes.selectors import company_indexes_as_of
from watchlists.selectors import get_active_watchlist_company_ids

MONITORING_POOL_COMPOSITION_REASON = "Monitoring pool v2 composition recompute."


@dataclass(frozen=True, slots=True)
class CurrentMonitoringPoolComposition:
    selection: MonitoringPoolSelectionResult
    active_watchlist_company_ids: tuple[UUID, ...]
    affected_company_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class MonitoringCompositionRecomputeResult:
    composition: CurrentMonitoringPoolComposition
    recompute_results: tuple[CompanyMonitoringStatusResult, ...]

    @property
    def recomputed_company_ids(self) -> tuple[UUID, ...]:
        return tuple(result.company.pk for result in self.recompute_results)

    @property
    def changed_company_ids(self) -> tuple[UUID, ...]:
        return tuple(
            result.company.pk for result in self.recompute_results if result.status_changed
        )

    @property
    def unchanged_company_ids(self) -> tuple[UUID, ...]:
        return tuple(
            result.company.pk for result in self.recompute_results if not result.status_changed
        )


def compose_current_monitoring_pool(
    *,
    as_of: date,
    enabled_index_codes: Iterable[str],
) -> CurrentMonitoringPoolComposition:
    """Compose a v2 snapshot from current global watchlist and index facts."""

    active_watchlist_company_ids = get_active_watchlist_company_ids()
    selection = select_monitoring_pool(
        as_of=as_of,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
        enabled_index_codes=enabled_index_codes,
        watchlist_company_ids=active_watchlist_company_ids,
    )
    affected_company_ids = _affected_company_ids(
        selection=selection,
        active_watchlist_company_ids=active_watchlist_company_ids,
    )
    return CurrentMonitoringPoolComposition(
        selection=selection,
        active_watchlist_company_ids=active_watchlist_company_ids,
        affected_company_ids=affected_company_ids,
    )


def recompute_monitoring_pool_composition(
    *,
    composition: CurrentMonitoringPoolComposition,
    sync_run: SyncRun,
    reason: str = MONITORING_POOL_COMPOSITION_REASON,
    request_id: str | None = None,
) -> MonitoringCompositionRecomputeResult:
    """Recompute every affected Company from authoritative as-of facts."""

    affected_company_ids = composition.affected_company_ids
    if not affected_company_ids:
        return MonitoringCompositionRecomputeResult(composition, ())

    active_watchlist_company_ids = set(
        get_active_watchlist_company_ids(company_ids=affected_company_ids)
    )
    stable_request_id = request_id or (
        f"monitoring-pool-composition:{sync_run.pk}:{composition.selection.snapshot.pk}"
    )
    results: list[CompanyMonitoringStatusResult] = []
    with transaction.atomic():
        for company_id in affected_company_ids:
            identity_pending = company_identity_pending_as_of(
                company_id=company_id,
                as_of=composition.selection.as_of_date,
            )
            has_enabled_index_membership = company_indexes_as_of(
                company_id=company_id,
                as_of_date=composition.selection.as_of_date,
                is_enabled=True,
            ).exists()
            results.append(
                recalculate_company_monitoring_status(
                    company_id=company_id,
                    as_of=composition.selection.as_of_date,
                    has_enabled_index_membership=has_enabled_index_membership,
                    has_active_watchlist=company_id in active_watchlist_company_ids,
                    identity_pending=identity_pending,
                    sync_run=sync_run,
                    reason=reason,
                    request_id=stable_request_id,
                )
            )
    return MonitoringCompositionRecomputeResult(composition, tuple(results))


def _affected_company_ids(
    *,
    selection: MonitoringPoolSelectionResult,
    active_watchlist_company_ids: tuple[UUID, ...],
) -> tuple[UUID, ...]:
    current_pool_company_ids = {member.company_id for member in selection.members}
    tracked_company_ids = set(
        Company.objects.filter(
            Q(
                monitoring_status__in=(
                    Company.MonitoringStatus.ACTIVE,
                    Company.MonitoringStatus.PENDING_IDENTITY,
                )
            )
            | Q(cik__isnull=True)
        ).values_list("id", flat=True)
    )
    prior_pool_company_ids = _latest_prior_v2_pool_company_ids(selection)
    return tuple(
        sorted(
            current_pool_company_ids
            | set(active_watchlist_company_ids)
            | tracked_company_ids
            | prior_pool_company_ids,
            key=str,
        )
    )


def _latest_prior_v2_pool_company_ids(
    selection: MonitoringPoolSelectionResult,
) -> set[UUID]:
    current_snapshot = selection.snapshot
    prior_snapshots = MonitoringPoolSnapshot.objects.filter(
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
        as_of_date__lte=current_snapshot.as_of_date,
    ).exclude(pk=current_snapshot.pk)
    prior_as_of = (
        prior_snapshots.order_by("-as_of_date").values_list("as_of_date", flat=True).first()
    )
    if prior_as_of is None:
        return set()
    return set(
        MonitoringPoolMember.objects.filter(
            snapshot__selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
            snapshot__as_of_date=prior_as_of,
        )
        .exclude(snapshot=current_snapshot)
        .values_list("company_id", flat=True)
    )
