"""Stage 6.3 tests: in-app notifications and settings pages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from django.test import Client

from accounts.models import User
from companies.models import Company
from notifications.models import Notification, ReminderRule, UserNotificationPreference
from tests.pages.helpers import company_with_listing, login_client, make_filing, make_user

pytestmark = pytest.mark.django_db


def _notification(
    user: User,
    company: Company,
    *,
    suffix: str = "page",
    status: str = "pending",
    priority: str = "P0",
    read_at: datetime | None = None,
    source_url: str | None = "https://example.test/source",
) -> Notification:
    filing = make_filing(company, accepted_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    return Notification.objects.create(
        user=user,
        event_type=ReminderRule.EventType.FILING,
        related_object_type=Notification.RelatedObjectType.FILING,
        related_object_id=filing.pk,
        event_version=f"v-{suffix}",
        priority=priority,
        channel=ReminderRule.Channel.IN_APP,
        status=status,
        idempotency_key=uuid.uuid4().hex,
        suppressed_reason="rule_disabled" if status == "suppressed" else None,
        source_url=source_url,
        read_at=read_at,
    )


class TestNotificationsPage:
    def test_requires_login(self) -> None:
        response = Client().get("/notifications/")
        assert response.status_code == 302
        assert response["Location"].startswith("/admin/login/")

    def test_renders_fields_and_source_link(self) -> None:
        user = make_user("render")
        company, _listing = company_with_listing("NOTIF")
        _notification(user, company, suffix="render")

        content = login_client(user).get("/notifications/").content.decode()

        assert company.display_name in content
        assert "SEC filing" in content
        assert "P0" in content
        assert "UTC" in content
        assert "https://example.test/source" in content
        assert "Unread" in content
        assert "Mark read" in content

    def test_renders_suppressed_reason_and_read_state(self) -> None:
        user = make_user("suppressed")
        company, _listing = company_with_listing("SUPP")
        _notification(
            user,
            company,
            suffix="supp",
            status="suppressed",
            read_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        )

        content = login_client(user).get("/notifications/").content.decode()

        assert "Suppressed" in content
        assert "rule_disabled" in content
        assert "Read" in content
        assert "Mark read" not in content

    def test_hides_other_users_notifications(self) -> None:
        owner = make_user("owner")
        other = make_user("other")
        owned_company, _l1 = company_with_listing("MINE6")
        other_company, _l2 = company_with_listing("YOURS")
        _notification(owner, owned_company, suffix="mine")
        _notification(other, other_company, suffix="theirs")

        content = login_client(owner).get("/notifications/").content.decode()

        assert owned_company.display_name in content
        assert other_company.display_name not in content

    def test_empty_state(self) -> None:
        user = make_user("empty")
        content = login_client(user).get("/notifications/").content.decode()
        assert "No notifications yet" in content

    def test_user_timezone_renders_local_line(self) -> None:
        user = make_user("tz")
        user.timezone = "Asia/Tokyo"
        user.save(update_fields=("timezone",))
        company, _listing = company_with_listing("TZX")
        _notification(user, company, suffix="tz")

        content = login_client(user).get("/notifications/").content.decode()

        assert "JST" in content


class TestMarkRead:
    def test_marks_read_and_is_idempotent(self) -> None:
        user = make_user("read")
        company, _listing = company_with_listing("READ")
        notification = _notification(user, company, suffix="read")
        client = login_client(user)

        response = client.post("/notifications/read/", {"notification_id": str(notification.pk)})
        assert response.status_code == 302
        notification.refresh_from_db()
        first_read_at = notification.read_at
        assert first_read_at is not None

        client.post("/notifications/read/", {"notification_id": str(notification.pk)})
        notification.refresh_from_db()
        assert notification.read_at == first_read_at

    def test_read_mark_is_independent_of_send_status(self) -> None:
        user = make_user("read-supp")
        company, _listing = company_with_listing("RSUP")
        notification = _notification(user, company, suffix="supp", status="suppressed")

        login_client(user).post("/notifications/read/", {"notification_id": str(notification.pk)})
        notification.refresh_from_db()

        assert notification.read_at is not None
        assert notification.status == Notification.Status.SUPPRESSED

    def test_foreign_notification_behaves_as_not_found(self) -> None:
        owner = make_user("foreign-owner")
        other = make_user("foreign-other")
        company, _listing = company_with_listing("FRGN")
        notification = _notification(owner, company, suffix="foreign")

        response = login_client(other).post(
            "/notifications/read/", {"notification_id": str(notification.pk)}
        )

        assert response.status_code == 302
        assert "error=notification_not_found" in response["Location"]
        notification.refresh_from_db()
        assert notification.read_at is None

    def test_non_post_redirects(self) -> None:
        user = make_user("read-get")
        response = login_client(user).get("/notifications/read/")
        assert response.status_code == 302

    def test_requires_csrf(self) -> None:
        user = make_user("csrf")
        company, _listing = company_with_listing("CSRF")
        notification = _notification(user, company, suffix="csrf")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post("/notifications/read/", {"notification_id": str(notification.pk)})

        assert response.status_code == 403
        notification.refresh_from_db()
        assert notification.read_at is None


class TestNotificationSettingsPage:
    def test_requires_login(self) -> None:
        response = Client().get("/settings/notifications/")
        assert response.status_code == 302
        assert response["Location"].startswith("/admin/login/")

    def test_renders_all_event_types_and_channels(self) -> None:
        user = make_user("settings-render")
        content = login_client(user).get("/settings/notifications/").content.decode()

        assert "Email notifications" in content
        assert "In-app notifications" in content
        for _value, label in ReminderRule.EventType.choices:
            assert label in content
        assert "Display timezone" in content
        # 6.4 capability must not appear as an input control.
        assert 'name="digest' not in content
        assert "Digest scheduling is not available yet." in content

    def test_save_is_idempotent_across_repeated_submissions(self) -> None:
        user = make_user("settings-save")
        client = login_client(user)
        payload = {
            "email_enabled": "on",
            "in_app_enabled": "on",
            "event_filing": "on",
            "event_date_changed": "on",
            "default_lead_time_days": "2",
            "timezone": "Asia/Tokyo",
        }

        client.post("/settings/notifications/", payload)
        client.post("/settings/notifications/", payload)

        preference = UserNotificationPreference.objects.get(user=user)
        assert preference.email_enabled is True
        assert preference.in_app_enabled is True
        assert preference.default_lead_time_minutes == 2880
        user.refresh_from_db()
        assert user.timezone == "Asia/Tokyo"
        rules = ReminderRule.objects.filter(user=user, company__isnull=True)
        # Every type is materialized (unchecked boxes are explicit offs).
        assert rules.count() == 16
        enabled_pairs = {(rule.event_type, rule.channel) for rule in rules if rule.is_enabled}
        assert enabled_pairs == {
            ("filing", "email"),
            ("filing", "in_app"),
            ("date_changed", "email"),
            ("date_changed", "in_app"),
        }

    def test_disable_creates_disabled_rules_and_reenable_flips(self) -> None:
        user = make_user("settings-toggle")
        client = login_client(user)

        off_payload = {
            "in_app_enabled": "on",
            "default_lead_time_days": "1",
            "timezone": "",
        }
        client.post("/settings/notifications/", off_payload)

        rules = ReminderRule.objects.filter(user=user, company__isnull=True)
        assert rules.count() == 16  # 8 types x 2 channels, all disabled
        assert all(rule.is_enabled is False for rule in rules)

        on_payload = dict(off_payload)
        on_payload["event_filing"] = "on"
        client.post("/settings/notifications/", on_payload)

        filing_rules = rules.filter(event_type=ReminderRule.EventType.FILING)
        assert filing_rules.count() == 2
        assert all(rule.is_enabled is True for rule in filing_rules)
        assert all(
            rule.is_enabled is False
            for rule in rules.exclude(event_type=ReminderRule.EventType.FILING)
        )

    def test_invalid_timezone_rejects_and_keeps_old_value(self) -> None:
        user = make_user("settings-tz")
        user.timezone = "Asia/Tokyo"
        user.save(update_fields=("timezone",))
        client = login_client(user)

        response = client.post(
            "/settings/notifications/",
            {"timezone": "Mars/Olympus_Mons", "default_lead_time_days": "1"},
        )

        assert response.status_code == 302
        assert "error=invalid_settings" in response["Location"]
        user.refresh_from_db()
        assert user.timezone == "Asia/Tokyo"

    def test_invalid_lead_days_rejects(self) -> None:
        user = make_user("settings-lead")
        response = login_client(user).post(
            "/settings/notifications/",
            {"default_lead_time_days": "0", "timezone": ""},
        )

        assert response.status_code == 302
        assert "error=invalid_settings" in response["Location"]
        assert UserNotificationPreference.objects.filter(user=user).exists() is False

    def test_save_requires_csrf(self) -> None:
        user = make_user("settings-csrf")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post("/settings/notifications/", {"timezone": ""})

        assert response.status_code == 403

    def test_saved_note_renders_after_prg(self) -> None:
        user = make_user("settings-note")
        client = login_client(user)

        response = client.post(
            "/settings/notifications/",
            {"in_app_enabled": "on", "default_lead_time_days": "1", "timezone": ""},
        )

        content = client.get(response["Location"]).content.decode()
        assert "Settings saved." in content
