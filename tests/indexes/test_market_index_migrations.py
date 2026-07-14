from __future__ import annotations

from importlib import import_module
from io import StringIO

import pytest
from django.apps import apps as global_apps
from django.core.management import call_command

from audit.models import AuditRecord, DataChange
from indexes.models import MarketIndex


def _run_forward() -> None:
    call_command(
        "migrate",
        "indexes",
        "0001_initial_market_index",
        "--noinput",
        stdout=StringIO(),
    )
    call_command(
        "migrate",
        "indexes",
        "0002_seed_builtin_indexes",
        "--noinput",
        stdout=StringIO(),
    )


def _run_reverse() -> None:
    call_command(
        "migrate",
        "indexes",
        "0001_initial_market_index",
        "--noinput",
        stdout=StringIO(),
    )


def _call_seed_forward() -> None:
    seed_module = import_module("indexes.migrations.0002_seed_builtin_indexes")
    seed_module.seed_builtin_indexes(global_apps, None)


class TestSeedMigration:
    def test_seed_creates_exactly_four_indexes(self, db: object) -> None:
        del db
        assert MarketIndex.objects.count() == 4

    def test_correct_codes_after_seed(self, db: object) -> None:
        del db
        codes = set(MarketIndex.objects.values_list("code", flat=True))
        assert codes == {"SP500", "NASDAQ100", "DJIA", "RUSSELL2000"}

    def test_correct_names_after_seed(self, db: object) -> None:
        del db
        name_map = dict(MarketIndex.objects.values_list("code", "name"))
        assert name_map["SP500"] == "S&P 500"
        assert name_map["NASDAQ100"] == "Nasdaq 100"
        assert name_map["DJIA"] == "Dow Jones Industrial Average"
        assert name_map["RUSSELL2000"] == "Russell 2000"

    def test_correct_groups_after_seed(self, db: object) -> None:
        del db
        group_map = dict(MarketIndex.objects.values_list("code", "index_group"))
        assert group_map["SP500"] == "LARGE"
        assert group_map["NASDAQ100"] == "LARGE"
        assert group_map["DJIA"] == "LARGE"
        assert group_map["RUSSELL2000"] == "SMALL"

    def test_default_is_enabled_after_seed(self, db: object) -> None:
        del db
        assert MarketIndex.objects.filter(is_enabled=True).count() == 4

    def test_reverse_and_reapply_restores_defaults(self, db: object) -> None:
        """reverse → forward resets is_enabled to default (True),
        because reverse deletes and forward creates fresh records."""
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        sp500.is_enabled = False
        sp500.save(update_fields={"is_enabled", "updated_at"})
        assert MarketIndex.objects.get(code="SP500").is_enabled is False

        _run_reverse()
        assert MarketIndex.objects.count() == 0

        _run_forward()
        assert MarketIndex.objects.count() == 4
        sp500_refreshed = MarketIndex.objects.get(code="SP500")
        assert sp500_refreshed.is_enabled is True

    def test_direct_seed_rerun_preserves_is_enabled(self, db: object) -> None:
        """Direct re-invocation of seed_builtin_indexes must not reset is_enabled."""
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_id = sp500.pk
        sp500.is_enabled = False
        sp500.save(update_fields={"is_enabled", "updated_at"})

        _call_seed_forward()

        sp500.refresh_from_db()
        assert sp500.is_enabled is False, "seed re-run must preserve modified is_enabled"
        assert sp500.pk == original_id, "UUID must not change"
        assert MarketIndex.objects.count() == 4, "still exactly four indexes"

    def test_direct_seed_rerun_preserves_ids(self, db: object) -> None:
        """Direct re-invocation must not change UUIDs."""
        del db
        original_ids = set(MarketIndex.objects.values_list("id", flat=True))
        assert len(original_ids) == 4

        _call_seed_forward()

        rerun_ids = set(MarketIndex.objects.values_list("id", flat=True))
        assert rerun_ids == original_ids, "UUIDs must not change on seed re-run"
        assert MarketIndex.objects.count() == 4

    def test_reverse_and_reapply_preserves_code_set(self, db: object) -> None:
        del db
        original_codes = set(MarketIndex.objects.values_list("code", flat=True))
        _run_reverse()
        _run_forward()
        assert MarketIndex.objects.count() == 4
        rerun_codes = set(MarketIndex.objects.values_list("code", flat=True))
        assert rerun_codes == original_codes

    def test_seed_detects_inconsistent_name(self, db: object) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        sp500.name = "Wrong Name"
        sp500.save(update_fields={"name", "updated_at"})
        with pytest.raises(ValueError, match="inconsistent name or group"):
            _call_seed_forward()

    def test_seed_accepts_consistent_data(self, db: object) -> None:
        del db
        _call_seed_forward()

    def test_reverse_removes_builtin_indexes(self, db: object) -> None:
        del db
        assert MarketIndex.objects.count() == 4
        _run_reverse()
        assert MarketIndex.objects.count() == 0
        _run_forward()
        assert MarketIndex.objects.count() == 4

    def test_seed_does_not_create_data_change(self, db: object) -> None:
        del db
        market_index_ids = set(MarketIndex.objects.values_list("id", flat=True))
        dc_count = DataChange.objects.filter(
            target_type="market_index",
            target_id__in=market_index_ids,
        ).count()
        assert dc_count == 0, "Seed migration should not create DataChange records."

    def test_seed_does_not_create_audit_record(self, db: object) -> None:
        del db
        market_index_ids = set(MarketIndex.objects.values_list("id", flat=True))
        ar_count = AuditRecord.objects.filter(
            target_type="market_index",
            target_id__in=market_index_ids,
        ).count()
        assert ar_count == 0, "Seed migration should not create AuditRecord records."
