"""Stage 7.2 read-only admin registration for index change facts."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from indexes.admin import (
    IndexChangeCorrelationAdmin,
    IndexChangeEventAdmin,
    IndexChangeLegAdmin,
)
from indexes.models import (
    IndexChangeCorrelation,
    IndexChangeEvent,
    IndexChangeLeg,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("model", "admin_class"),
    (
        (IndexChangeEvent, IndexChangeEventAdmin),
        (IndexChangeLeg, IndexChangeLegAdmin),
        (IndexChangeCorrelation, IndexChangeCorrelationAdmin),
    ),
)
def test_index_change_admins_are_read_only(
    model: type[IndexChangeEvent] | type[IndexChangeLeg] | type[IndexChangeCorrelation],
    admin_class: (
        type[IndexChangeEventAdmin] | type[IndexChangeLegAdmin] | type[IndexChangeCorrelationAdmin]
    ),
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/indexes/")

    assert set(model_admin.readonly_fields) == {field.name for field in model._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_index_change_event_admin_filters_and_search() -> None:
    model_admin = IndexChangeEventAdmin(IndexChangeEvent, AdminSite())

    assert {"status", "displacement", "monitoring_impact"} <= set(model_admin.list_filter)
    assert {"company__display_name", "=company__cik"} <= set(model_admin.search_fields)


def test_index_change_correlation_admin_filters_pending_review() -> None:
    model_admin = IndexChangeCorrelationAdmin(IndexChangeCorrelation, AdminSite())

    # pending correlations are the cross-date review queue (ADR-006)
    assert "status" in model_admin.list_filter
