"""Stage 7.2 admin manual company correction: permission, reason and audit."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory

from accounts.models import User
from audit.models import AuditRecord, DataChange
from companies.admin import CompanyAdmin
from companies.models import Company
from companies.services import update_company

CORRECT_URL = "/admin/companies/company/{pk}/correct/"


def make_staff(suffix: str, *, permission: bool) -> User:
    user = User.objects.create_user(
        email=f"{suffix}-{uuid.uuid4().hex[:8]}@example.test",
        password="test",
        is_staff=True,
    )
    # every admin-visible role needs the built-in read permission to open the
    # changelist; the custom permission gates only the write action
    user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="companies",
            codename="view_company",
        )
    )
    if permission:
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="companies",
                codename="manual_correction",
            )
        )
    return user


@pytest.fixture
def company() -> Company:
    return Company.objects.create(
        cik="0000123456",
        legal_name="Original Legal",
        display_name="Original Display",
        country_code="US",
    )


def _post_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "legal_name": "Original Legal",
        "display_name": "Original Display",
        "country_code": "US",
        "issuer_type": "unknown",
        "fiscal_year_end_month_day": "",
        "investor_relations_url": "",
        "reason": "Fixture correction reason.",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_correction_records_before_after_and_actor(client: Client, company: Company) -> None:
    staff = make_staff("correct", permission=True)
    client.force_login(staff)

    response = client.post(
        CORRECT_URL.format(pk=company.pk),
        _post_data(display_name="Corrected Display"),
    )

    assert response.status_code == 302
    company.refresh_from_db()
    assert company.display_name == "Corrected Display"
    change = DataChange.objects.select_related("actor_user").get(
        target_type=DataChange.TargetType.COMPANY,
        target_id=company.pk,
        field_name="display_name",
    )
    assert change.old_value == "Original Display"
    assert change.new_value == "Corrected Display"
    assert change.actor_user == staff
    assert change.reason == "Fixture correction reason."
    record = AuditRecord.objects.get(
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=company.pk,
        actor_user=staff,
    )
    assert record.before is not None
    assert record.before["display_name"] == "Original Display"
    assert record.after is not None
    assert record.after["display_name"] == "Corrected Display"
    assert record.reason == "Fixture correction reason."


@pytest.mark.django_db
def test_correction_reason_is_required(client: Client, company: Company) -> None:
    staff = make_staff("reason", permission=True)
    client.force_login(staff)

    response = client.post(
        CORRECT_URL.format(pk=company.pk),
        _post_data(display_name="Corrected Display", reason=""),
    )

    assert response.status_code == 200
    assert "reason" in response.context["form"].errors
    assert not DataChange.objects.filter(target_id=company.pk).exists()
    company.refresh_from_db()
    assert company.display_name == "Original Display"


@pytest.mark.django_db
def test_unchanged_correction_creates_no_audit_rows(client: Client, company: Company) -> None:
    staff = make_staff("idem", permission=True)
    client.force_login(staff)

    response = client.post(CORRECT_URL.format(pk=company.pk), _post_data())

    assert response.status_code == 200
    assert response.context["form"].non_field_errors
    company.refresh_from_db()
    assert company.display_name == "Original Display"
    assert not DataChange.objects.filter(target_id=company.pk).exists()
    assert not AuditRecord.objects.filter(
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_id=company.pk,
    ).exists()
    # the service itself stays idempotent for identical reruns
    result = update_company(
        company=company,
        changes={"display_name": "Original Display"},
        actor_user=staff,
        reason="rerun",
        request_id="admin-correction-rerun",
    )
    assert result.created is False
    assert result.data_changes == ()
    assert result.audit_record is None


@pytest.mark.django_db
def test_correction_view_denies_staff_without_permission(client: Client, company: Company) -> None:
    staff = make_staff("viewer", permission=False)
    client.force_login(staff)

    response = client.get(CORRECT_URL.format(pk=company.pk))

    assert response.status_code == 403


@pytest.mark.django_db
def test_correction_view_redirects_non_staff_to_login(client: Client, company: Company) -> None:
    response = client.get(CORRECT_URL.format(pk=company.pk))

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


@pytest.mark.django_db
def test_correction_action_hidden_without_permission() -> None:
    model_admin = CompanyAdmin(Company, AdminSite())
    request = RequestFactory().get("/admin/companies/company/")
    request.user = make_staff("actions", permission=False)

    actions = model_admin.get_actions(request)

    assert "correct_companies" not in actions

    privileged = RequestFactory().get("/admin/companies/company/")
    privileged.user = make_staff("actions-ok", permission=True)
    assert "correct_companies" in model_admin.get_actions(privileged)


@pytest.mark.django_db
def test_correction_action_requires_exactly_one_selection(client: Client, company: Company) -> None:
    staff = make_staff("single", permission=True)
    client.force_login(staff)
    other = Company.objects.create(
        cik="0000654321", legal_name="Other Legal", display_name="Other Display"
    )

    response = client.post(
        "/admin/companies/company/",
        {"action": "correct_companies", "_selected_action": [str(company.pk), str(other.pk)]},
    )
    assert response.status_code == 302  # stays on changelist with a warning
    assert not AuditRecord.objects.filter(
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.COMPANY,
    ).exists()

    response = client.post(
        "/admin/companies/company/",
        {"action": "correct_companies", "_selected_action": [str(company.pk)]},
    )
    assert response.status_code == 302
    assert response["Location"].endswith(f"/admin/companies/company/{company.pk}/correct/")
