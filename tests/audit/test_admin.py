import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.models import User
from audit.admin import (
    AuditRecordAdmin,
    DataChangeAdmin,
    DataSourceAdmin,
    RawDataObservationAdmin,
    RawDataRecordAdmin,
    SourceEvidenceAdmin,
    SyncRunAdmin,
)
from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)


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


@pytest.mark.django_db
def test_data_source_admin_rejects_base_url_credentials() -> None:
    model_admin = DataSourceAdmin(DataSource, AdminSite())
    request = RequestFactory().post("/admin/audit/datasource/add/")
    request.user = User.objects.create_superuser(
        email="datasource-admin@example.com",
        password="fixture-password-only",
    )
    form_class = model_admin.get_form(request)
    form = form_class(
        data={
            "key": "unsafe-admin-source",
            "name": "Unsafe admin source",
            "source_type": DataSource.SourceType.MANUAL,
            "base_url": "https://example.test/data?auth=fixture-admin-secret",
            "provider_adapter": "",
            "license_notes": "",
            "is_enabled": "on",
        }
    )

    assert form.is_valid() is False
    assert "fixture-admin-secret" not in str(form.errors)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "admin_class", "json_fields", "preview_fields"),
    (
        (
            DataChange,
            DataChangeAdmin,
            {"old_value", "new_value"},
            {"old_value_preview", "new_value_preview"},
        ),
        (
            AuditRecord,
            AuditRecordAdmin,
            {"before", "after"},
            {"before_preview", "after_preview"},
        ),
    ),
)
def test_change_history_admins_are_read_only_and_hide_full_json(
    model: type[DataChange] | type[AuditRecord],
    admin_class: type[DataChangeAdmin] | type[AuditRecordAdmin],
    json_fields: set[str],
    preview_fields: set[str],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/audit/history/")
    request.user = User.objects.create_superuser(
        email=f"{model._meta.model_name}-admin@example.com",
        password="fixture-password-only",
    )

    expected_readonly = {
        field.name for field in model._meta.fields if field.name not in json_fields
    } | preview_fields
    assert set(model_admin.exclude or ()) == json_fields
    assert set(model_admin.readonly_fields) == expected_readonly
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_change_history_admin_previews_are_truncated() -> None:
    data_change_admin = DataChangeAdmin(DataChange, AdminSite())
    audit_record_admin = AuditRecordAdmin(AuditRecord, AdminSite())
    long_value = {"safe_value": "x" * 500}

    assert len(data_change_admin.old_value_preview(DataChange(old_value=long_value))) == 240
    assert len(audit_record_admin.before_preview(AuditRecord(before=long_value))) == 240


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "admin_class"),
    ((DataChange, DataChangeAdmin), (AuditRecord, AuditRecordAdmin)),
)
def test_change_history_admins_require_view_permission(
    model: type[DataChange] | type[AuditRecord],
    admin_class: type[DataChangeAdmin] | type[AuditRecordAdmin],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/audit/history/")
    request.user = User.objects.create_user(
        email=f"{model._meta.model_name}-staff@example.com",
        password="fixture-password-only",
        is_staff=True,
    )

    assert model_admin.has_view_permission(request) is False
