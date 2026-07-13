import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from audit.admin import SyncRunAdmin
from audit.models import SyncRun


@pytest.mark.django_db
def test_sync_run_admin_is_read_only() -> None:
    model_admin = SyncRunAdmin(SyncRun, AdminSite())
    request = RequestFactory().get("/admin/audit/syncrun/")

    assert set(model_admin.readonly_fields) == {field.name for field in SyncRun._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
