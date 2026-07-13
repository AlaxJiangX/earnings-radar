import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.models import User
from audit.admin import (
    RawDataObservationAdmin,
    RawDataRecordAdmin,
    SourceEvidenceAdmin,
    SyncRunAdmin,
)
from audit.models import RawDataObservation, RawDataRecord, SourceEvidence, SyncRun


@pytest.mark.django_db
def test_sync_run_admin_is_read_only() -> None:
    model_admin = SyncRunAdmin(SyncRun, AdminSite())
    request = RequestFactory().get("/admin/audit/syncrun/")

    assert set(model_admin.readonly_fields) == {field.name for field in SyncRun._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "admin_class"),
    (
        (RawDataRecord, RawDataRecordAdmin),
        (RawDataObservation, RawDataObservationAdmin),
    ),
)
def test_raw_data_admins_are_read_only(
    model: type[RawDataRecord] | type[RawDataObservation],
    admin_class: type[RawDataRecordAdmin] | type[RawDataObservationAdmin],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/audit/raw-data/")

    expected_fields = {field.name for field in model._meta.fields}
    if model is RawDataRecord:
        expected_fields.remove("payload")
        assert model_admin.exclude == ("payload",)
    assert set(model_admin.readonly_fields) == expected_fields
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_source_evidence_admin_is_read_only_even_for_superuser() -> None:
    model_admin = SourceEvidenceAdmin(SourceEvidence, AdminSite())
    request = RequestFactory().get("/admin/audit/sourceevidence/")
    request.user = User.objects.create_superuser(
        email="admin@example.com",
        password="fixture-password-only",
    )

    assert set(model_admin.readonly_fields) == {field.name for field in SourceEvidence._meta.fields}
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_source_evidence_admin_requires_view_permission() -> None:
    model_admin = SourceEvidenceAdmin(SourceEvidence, AdminSite())
    request = RequestFactory().get("/admin/audit/sourceevidence/")
    request.user = User.objects.create_user(
        email="staff-without-permission@example.com",
        password="fixture-password-only",
        is_staff=True,
    )

    assert model_admin.has_view_permission(request) is False
