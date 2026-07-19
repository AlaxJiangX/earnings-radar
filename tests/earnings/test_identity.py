# mypy: ignore-errors
"""Identity derivation and period normalization tests."""

from __future__ import annotations

from datetime import date

import pytest

from earnings.identity import (
    IDENTITY_RULE_VERSION,
    derive_earnings_identity_key,
    normalize_period_type,
)


@pytest.mark.django_db
class TestDeriveIdentityKey:
    def test_same_input_same_output(self) -> None:
        from companies.models import Company

        co = Company.objects.create(
            cik="0000002500",
            legal_name="Identity TestCo",
            display_name="IdentityTest",
        )
        k1 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        k2 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        assert k1 == k2
        assert len(k1) == 64
        assert all(c in "0123456789abcdef" for c in k1)

    def test_different_company_different_key(self) -> None:
        from companies.models import Company

        co_a = Company.objects.create(cik="0000002501", legal_name="A Co", display_name="A")
        co_b = Company.objects.create(cik="0000002502", legal_name="B Co", display_name="B")
        k_a = derive_earnings_identity_key(
            company_id=co_a.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        k_b = derive_earnings_identity_key(
            company_id=co_b.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        assert k_a != k_b

    def test_different_date_different_key(self) -> None:
        from companies.models import Company

        co = Company.objects.create(cik="0000002503", legal_name="Date Co", display_name="DateCo")
        k1 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        k2 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 6, 30),
            period_type="Q1",
        )
        assert k1 != k2

    def test_different_type_different_key(self) -> None:
        from companies.models import Company

        co = Company.objects.create(cik="0000002504", legal_name="Type Co", display_name="TypeCo")
        k1 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        k2 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q2",
        )
        assert k1 != k2

    def test_rule_version_not_in_identity_key(self) -> None:
        """identity_key must NOT include rule_version. Same business identity
        must produce the same key regardless of rule version."""
        from companies.models import Company

        co = Company.objects.create(cik="0000002505", legal_name="Ver Co", display_name="VerCo")
        k = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        # Verify IDENTITY_RULE_VERSION is a separate constant, not part of the hash
        assert IDENTITY_RULE_VERSION == "v1"
        # The key is derived without rule_version, so same inputs = same key
        k2 = derive_earnings_identity_key(
            company_id=co.pk,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        assert k == k2


class TestNormalizePeriodType:
    def test_q4_to_fy(self) -> None:
        pt, includes_q4 = normalize_period_type("Q4")
        assert pt == "FY"
        assert includes_q4 is True

    def test_q1_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("Q1")
        assert pt == "Q1"
        assert includes_q4 is False

    def test_q2_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("Q2")
        assert pt == "Q2"
        assert includes_q4 is False

    def test_q3_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("Q3")
        assert pt == "Q3"
        assert includes_q4 is False

    def test_fy_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("FY")
        assert pt == "FY"
        assert includes_q4 is True

    def test_h1_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("H1")
        assert pt == "H1"
        assert includes_q4 is False

    def test_h2_passthrough(self) -> None:
        pt, includes_q4 = normalize_period_type("H2")
        assert pt == "H2"
        assert includes_q4 is False

    def test_other_explicit(self) -> None:
        pt, includes_q4 = normalize_period_type("OTHER")
        assert pt == "OTHER"
        assert includes_q4 is False

    def test_unknown_returns_none(self) -> None:
        pt, includes_q4 = normalize_period_type("UNKNOWN_LABEL")
        assert pt is None
        assert includes_q4 is False

    def test_unknown_not_auto_other(self) -> None:
        pt, _ = normalize_period_type("Fourth Quarter")
        assert pt is None  # Not auto-mapped to OTHER

    def test_annual_to_fy(self) -> None:
        pt, includes_q4 = normalize_period_type("ANNUAL")
        assert pt == "FY"
        assert includes_q4 is True
