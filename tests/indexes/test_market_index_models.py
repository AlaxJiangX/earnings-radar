import pytest
from django.db import IntegrityError, transaction

from indexes.models import ALLOWED_CODES, ALLOWED_INDEX_GROUPS, MarketIndex


class TestMarketIndexConstraints:
    """Tests for database-level constraints on MarketIndex.

    The seed migration creates four built-in indexes, so these tests
    either delete seed data first or use get/create patterns respecting
    the unique constraint on code.
    """

    @pytest.fixture(autouse=True)
    def clear_seed_data(self, db: object) -> None:
        """Remove seed data so constraint tests start from a clean table."""
        del db
        MarketIndex.objects.all().delete()

    def test_create_valid_codes(self, db: object) -> None:
        del db
        for code, name, group in [
            ("SP500", "S&P 500", "LARGE"),
            ("NASDAQ100", "Nasdaq 100", "LARGE"),
            ("DJIA", "Dow Jones Industrial Average", "LARGE"),
            ("RUSSELL2000", "Russell 2000", "SMALL"),
        ]:
            index = MarketIndex.objects.create(
                code=code, name=name, index_group=group, is_enabled=True
            )
            assert index.code == code

    def test_code_unique(self, db: object) -> None:
        del db
        MarketIndex.objects.create(
            code="SP500", name="S&P 500", index_group="LARGE", is_enabled=True
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="SP500", name="S&P 500 Duplicate", index_group="LARGE", is_enabled=True
                )

    def test_fifth_code_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="FTSE100", name="FTSE 100", index_group="LARGE", is_enabled=True
                )

    def test_blank_code_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="   ", name="Blank Code", index_group="LARGE", is_enabled=True
                )

    def test_blank_name_rejected(self, db: object) -> None:
        del db
        for name in ["", "   "]:
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    MarketIndex.objects.create(
                        code="SP500", name=name, index_group="LARGE", is_enabled=True
                    )

    def test_invalid_index_group_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="SP500", name="S&P 500", index_group="MICRO", is_enabled=True
                )

    def test_sp500_with_small_group_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="SP500", name="S&P 500", index_group="SMALL", is_enabled=True
                )

    def test_nasdaq100_with_small_group_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="NASDAQ100", name="Nasdaq 100", index_group="SMALL", is_enabled=True
                )

    def test_djia_with_small_group_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="DJIA", name="Dow Jones Industrial Average", index_group="SMALL"
                )

    def test_russell2000_with_large_group_rejected(self, db: object) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                MarketIndex.objects.create(
                    code="RUSSELL2000", name="Russell 2000", index_group="LARGE", is_enabled=True
                )

    def test_valid_code_group_combinations(self, db: object) -> None:
        del db
        for code, group in [
            ("SP500", "LARGE"),
            ("NASDAQ100", "LARGE"),
            ("DJIA", "LARGE"),
            ("RUSSELL2000", "SMALL"),
        ]:
            MarketIndex.objects.create(code=code, name=code, index_group=group, is_enabled=True)

    def test_default_is_enabled(self, db: object) -> None:
        del db
        index = MarketIndex.objects.create(code="SP500", name="S&P 500", index_group="LARGE")
        assert index.is_enabled is True


class TestMarketIndexAllowedValues:
    """Verify module-level constants match TextChoices."""

    def test_allowed_codes_match(self) -> None:
        assert ALLOWED_CODES == frozenset(MarketIndex.Code.values)
        assert len(ALLOWED_CODES) == 4

    def test_allowed_groups_match(self) -> None:
        assert ALLOWED_INDEX_GROUPS == frozenset(MarketIndex.IndexGroup.values)
        assert len(ALLOWED_INDEX_GROUPS) == 2
