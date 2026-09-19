# mypy: ignore-errors
"""Migration verification for EarningsEvent precision and EarningsDateChange."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("earnings", "0002_fix_nullable_period_invariants")
MIGRATE_TO = ("earnings", "0003_earningsdatechange_and_more")


@pytest.mark.django_db(transaction=True)
def test_0003_backfills_existing_schedule_precision_and_release_session() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        Company = old_apps.get_model("companies", "Company")
        EarningsEvent = old_apps.get_model("earnings", "EarningsEvent")

        company = Company.objects.create(
            id=uuid.uuid4(),
            cik="0000094001",
            legal_name="Migration Precision Co",
            display_name="Migration Precision Co",
        )
        exact_at = datetime(2026, 10, 24, 20, 30, tzinfo=UTC)
        exact_event = EarningsEvent.objects.create(
            id=uuid.uuid4(),
            company=company,
            identity_status="candidate",
            status="scheduled_estimated",
            estimated_release_at=exact_at,
            release_session=None,
        )
        unknown_event = EarningsEvent.objects.create(
            id=uuid.uuid4(),
            company=company,
            identity_status="candidate",
            status="scheduled_estimated",
            release_session=None,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps
        EarningsEventAfter = new_apps.get_model("earnings", "EarningsEvent")

        exact_after = EarningsEventAfter.objects.get(pk=exact_event.pk)
        unknown_after = EarningsEventAfter.objects.get(pk=unknown_event.pk)

        assert exact_after.estimated_release_at == exact_at
        assert exact_after.estimated_release_date is None
        assert exact_after.estimated_release_precision == "exact_datetime"
        assert exact_after.release_session == "unknown"

        assert unknown_after.estimated_release_at is None
        assert unknown_after.estimated_release_date is None
        assert unknown_after.estimated_release_precision == "unknown"
        assert unknown_after.release_session == "unknown"
    finally:
        MigrationExecutor(connection).migrate(latest_targets)


def _historical_model_field_names(model: Any) -> set[str]:
    return {field.name for field in model._meta.fields}


@pytest.mark.django_db
def test_0003_model_contains_ratified_schedule_fields() -> None:
    from earnings.models import EarningsDateChange, EarningsEvent

    event_fields = _historical_model_field_names(EarningsEvent)
    assert {
        "estimated_release_date",
        "estimated_release_precision",
        "confirmed_release_date",
        "confirmed_release_precision",
        "earnings_release_date",
        "earnings_release_precision",
        "conference_call_date",
        "conference_call_precision",
    } <= event_fields

    change_fields = _historical_model_field_names(EarningsDateChange)
    assert {
        "field_name",
        "change_kind",
        "old_precision",
        "new_precision",
        "old_date",
        "new_date",
        "old_datetime",
        "new_datetime",
        "old_session",
        "new_session",
        "data_change",
        "detected_at",
        "created_at",
    } <= change_fields
