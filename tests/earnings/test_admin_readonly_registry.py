"""Stage 7.2 read-only registration red lines for new earnings admin models."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from earnings.admin import (
    EarningsCalendarObservationAdmin,
    EarningsReconciliationDecisionAdmin,
)
from earnings.models import EarningsCalendarObservation, EarningsReconciliationDecision

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("model", "admin_class"),
    (
        (EarningsCalendarObservation, EarningsCalendarObservationAdmin),
        (EarningsReconciliationDecision, EarningsReconciliationDecisionAdmin),
    ),
)
def test_new_earnings_admins_are_read_only(
    model: type[EarningsCalendarObservation] | type[EarningsReconciliationDecision],
    admin_class: type[EarningsCalendarObservationAdmin] | type[EarningsReconciliationDecisionAdmin],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/earnings/")

    assert set(model_admin.readonly_fields) == {field.name for field in model._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_reconciliation_decision_admin_filters_cover_conflict_queue() -> None:
    model_admin = EarningsReconciliationDecisionAdmin(EarningsReconciliationDecision, AdminSite())

    # open collision / conflict / review_required decisions are the
    # source-conflict queue surfaced by these two filters (ADR-014)
    assert {"decision_type", "status"} <= set(model_admin.list_filter)
    assert {"=decision_key", "=observation__id"} <= set(model_admin.search_fields)


def test_calendar_observation_admin_filters_and_search() -> None:
    model_admin = EarningsCalendarObservationAdmin(EarningsCalendarObservation, AdminSite())

    assert {"source", "period_type"} <= set(model_admin.list_filter)
    assert {"=cik", "ticker", "provider_event_id"} <= set(model_admin.search_fields)
