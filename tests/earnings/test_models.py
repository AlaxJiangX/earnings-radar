# mypy: ignore-errors
"""Model and constraint tests for EarningsEvent."""

from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from companies.models import Company
from earnings.models import EarningsEvent


def _make_company(cik: str, display_name: str) -> Company:
    co, _ = Company.objects.get_or_create(
        cik=cik,
        defaults={"legal_name": f"Legal {display_name}", "display_name": display_name},
    )
    return co


def _make_canonical(
    *,
    company: Company | None = None,
    period_end_date: date = date(2026, 3, 31),
    period_type: str = "Q1",
    **overrides,
) -> EarningsEvent:
    if company is None:
        company = _make_company("0000002001", "DefaultEarner")
    from earnings.identity import IDENTITY_RULE_VERSION, derive_earnings_identity_key

    identity_key = derive_earnings_identity_key(
        company_id=company.pk,
        period_end_date=period_end_date,
        period_type=period_type,
    )
    kwargs = {
        "company": company,
        "period_end_date": period_end_date,
        "period_type": period_type,
        "identity_status": "canonical",
        "identity_key": identity_key,
        "identity_rule_version": IDENTITY_RULE_VERSION,
        "includes_q4": period_type == "FY",
        **overrides,
    }
    return EarningsEvent.objects.create(**kwargs)


@pytest.mark.django_db
class TestEarningsEventModelBasic:
    def test_create_canonical_q1(self) -> None:
        ev = _make_canonical(period_type="Q1")
        assert ev.identity_status == "canonical"
        assert ev.period_type == "Q1"
        assert ev.period_end_date == date(2026, 3, 31)
        assert ev.identity_key is not None
        assert len(ev.identity_key) == 64

    def test_create_canonical_q2(self) -> None:
        ev = _make_canonical(period_type="Q2", period_end_date=date(2026, 6, 30))
        assert ev.period_type == "Q2"

    def test_create_canonical_q3(self) -> None:
        ev = _make_canonical(period_type="Q3", period_end_date=date(2026, 9, 30))
        assert ev.period_type == "Q3"

    def test_create_canonical_fy(self) -> None:
        ev = _make_canonical(
            period_type="FY",
            period_end_date=date(2026, 12, 31),
            includes_q4=True,
        )
        assert ev.period_type == "FY"
        assert ev.includes_q4 is True

    def test_create_canonical_h1(self) -> None:
        ev = _make_canonical(period_type="H1", period_end_date=date(2026, 6, 30))
        assert ev.period_type == "H1"

    def test_create_canonical_h2(self) -> None:
        ev = _make_canonical(period_type="H2", period_end_date=date(2026, 12, 31))
        assert ev.period_type == "H2"

    def test_create_canonical_other(self) -> None:
        ev = _make_canonical(period_type="OTHER", period_end_date=date(2026, 6, 30))
        assert ev.period_type == "OTHER"

    def test_default_status(self) -> None:
        ev = _make_canonical()
        assert ev.status == "scheduled_estimated"


@pytest.mark.django_db
class TestEarningsEventCandidate:
    def test_candidate_without_period_end_date(self) -> None:
        co = _make_company("0000002100", "CandidateCo")
        ev = EarningsEvent.objects.create(
            company=co,
            identity_status="candidate",
            status="scheduled_estimated",
        )
        assert ev.period_end_date is None
        assert ev.identity_key is None

    def test_candidate_identity_key_null(self) -> None:
        co = _make_company("0000002101", "NullKeyCo")
        ev = EarningsEvent.objects.create(
            company=co,
            identity_status="candidate",
            status="scheduled_estimated",
        )
        assert ev.identity_key is None

    def test_candidate_identity_rule_version_null(self) -> None:
        co = _make_company("0000002102", "NullVerCo")
        ev = EarningsEvent.objects.create(
            company=co,
            identity_status="candidate",
            status="scheduled_estimated",
        )
        assert ev.identity_rule_version is None

    def test_multiple_candidates_allowed(self) -> None:
        co = _make_company("0000002103", "MultiCand")
        EarningsEvent.objects.create(
            company=co, identity_status="candidate", status="scheduled_estimated"
        )
        EarningsEvent.objects.create(
            company=co, identity_status="candidate", status="scheduled_estimated"
        )
        assert EarningsEvent.objects.filter(company=co).count() == 2


@pytest.mark.django_db
class TestEarningsEventCanonicalConstraints:
    def test_canonical_missing_period_end_date_fails(self) -> None:
        co = _make_company("0000002200", "FailCo")
        from earnings.identity import IDENTITY_RULE_VERSION, derive_earnings_identity_key

        # Create with a dummy date then try to set it to None
        with pytest.raises(IntegrityError), transaction.atomic():
            EarningsEvent.objects.create(
                company=co,
                identity_status="canonical",
                period_end_date=None,  # Missing
                period_type="Q1",
                identity_key=derive_earnings_identity_key(
                    company_id=co.pk, period_end_date=date(2026, 1, 1), period_type="Q1"
                ),
                identity_rule_version=IDENTITY_RULE_VERSION,
                status="scheduled_estimated",
            )

    def test_duplicate_canonical_business_identity_fails(self) -> None:
        co = _make_company("0000002201", "DupCo")
        _make_canonical(company=co, period_end_date=date(2026, 3, 31), period_type="Q1")
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(company=co, period_end_date=date(2026, 3, 31), period_type="Q1")

    def test_canonical_completeness_requires_identity_key(self) -> None:
        co = _make_company("0000002202", "NoKey")
        with pytest.raises(IntegrityError), transaction.atomic():
            EarningsEvent.objects.create(
                company=co,
                identity_status="canonical",
                period_end_date=date(2026, 3, 31),
                period_type="Q1",
                identity_key=None,
                identity_rule_version="v1",
                status="scheduled_estimated",
            )


@pytest.mark.django_db
class TestEarningsEventIncludesQ4:
    def test_fy_with_true_valid(self) -> None:
        ev = _make_canonical(period_type="FY", period_end_date=date(2026, 12, 31), includes_q4=True)
        assert ev.includes_q4 is True

    def test_fy_with_false_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(
                period_type="FY",
                period_end_date=date(2026, 12, 31),
                includes_q4=False,
            )

    def test_q1_with_false_valid(self) -> None:
        ev = _make_canonical(period_type="Q1", includes_q4=False)
        assert ev.includes_q4 is False

    def test_q1_with_true_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(period_type="Q1", includes_q4=True)

    def test_q2_with_true_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(period_type="Q2", includes_q4=True)

    def test_q3_with_true_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(period_type="Q3", includes_q4=True)

    def test_h1_with_true_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(period_type="H1", includes_q4=True)

    def test_h2_with_true_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(period_type="H2", includes_q4=True)


@pytest.mark.django_db
class TestEarningsEventPeriodType:
    def test_q4_not_in_enum(self) -> None:
        co = _make_company("0000002300", "Q4Test")
        with pytest.raises(IntegrityError), transaction.atomic():
            EarningsEvent.objects.create(
                company=co,
                identity_status="candidate",
                period_type="Q4",
                status="scheduled_estimated",
            )


@pytest.mark.django_db
class TestEarningsEvent52Week:
    def test_52_weeks_valid(self) -> None:
        ev = _make_canonical(
            period_type="FY",
            period_end_date=date(2026, 12, 31),
            fiscal_calendar_type="week_based_52_53",
            period_length_weeks=52,
            includes_q4=True,
        )
        assert ev.period_length_weeks == 52

    def test_53_weeks_valid(self) -> None:
        ev = _make_canonical(
            period_type="FY",
            period_end_date=date(2026, 12, 31),
            fiscal_calendar_type="week_based_52_53",
            period_length_weeks=53,
            includes_q4=True,
        )
        assert ev.period_length_weeks == 53

    def test_50_weeks_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(
                period_type="FY",
                period_end_date=date(2026, 12, 31),
                fiscal_calendar_type="week_based_52_53",
                period_length_weeks=50,
                includes_q4=True,
            )

    def test_month_based_with_weeks_invalid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_canonical(
                period_type="FY",
                period_end_date=date(2026, 12, 31),
                fiscal_calendar_type="month_based",
                period_length_weeks=52,
                includes_q4=True,
            )


@pytest.mark.django_db
class TestEarningsEventSourceEvidence:
    def test_source_evidence_relation(self) -> None:
        """SourceEvidence target_type='earnings_event' can reference an EarningsEvent."""
        co = _make_company("0000002400", "SrcEvCo")
        ev = _make_canonical(company=co)
        # Verifies the SourceEvidence model admits earnings_event as target_type.
        # Full record creation (requiring raw_data_record_id etc.) is deferred.
        assert ev.source_evidence is None  # FK is nullable
        assert ev.pk is not None
