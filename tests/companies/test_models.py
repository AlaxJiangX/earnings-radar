from datetime import date

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from companies.models import Company, SecurityListing


@pytest.mark.django_db
def test_company_cik_is_unique_and_preserves_leading_zeroes() -> None:
    company = Company.objects.create(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="0000123456",
    )

    assert company.cik == "0000123456"
    with pytest.raises(IntegrityError), transaction.atomic():
        Company.objects.create(
            legal_name="Duplicate Fixture Incorporated",
            display_name="Duplicate Fixture",
            cik="0000123456",
        )


@pytest.mark.django_db
def test_company_database_constraints_reject_invalid_values() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Company.objects.create(
            legal_name=" ",
            display_name="Fixture",
            cik="invalid",
        )


@pytest.mark.django_db
def test_listing_constraints_reject_invalid_date_order() -> None:
    company = Company.objects.create(legal_name="Fixture Incorporated", display_name="Fixture")

    with pytest.raises(IntegrityError), transaction.atomic():
        SecurityListing.objects.create(
            company=company,
            ticker="FIX",
            exchange="XNAS",
            effective_from=date(2026, 1, 2),
            effective_to=date(2026, 1, 2),
        )


@pytest.mark.django_db
def test_same_exchange_and_ticker_cannot_have_overlapping_intervals() -> None:
    first = Company.objects.create(legal_name="First Incorporated", display_name="First")
    second = Company.objects.create(legal_name="Second Incorporated", display_name="Second")
    SecurityListing.objects.create(
        company=first,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SecurityListing.objects.create(
            company=second,
            ticker="FIX",
            exchange="XNAS",
            effective_from=date(2026, 2, 1),
        )


@pytest.mark.django_db
def test_historical_ticker_reuse_is_allowed_after_closed_interval() -> None:
    first = Company.objects.create(legal_name="First Incorporated", display_name="First")
    second = Company.objects.create(legal_name="Second Incorporated", display_name="Second")
    SecurityListing.objects.create(
        company=first,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2025, 1, 1),
        effective_to=date(2026, 1, 1),
    )
    reused = SecurityListing.objects.create(
        company=second,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
    )

    assert reused.effective_from == date(2026, 1, 1)


@pytest.mark.django_db
def test_company_has_at_most_one_primary_listing_at_a_time() -> None:
    company = Company.objects.create(legal_name="Fixture Incorporated", display_name="Fixture")
    SecurityListing.objects.create(
        company=company,
        ticker="FIXA",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        is_primary=True,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SecurityListing.objects.create(
            company=company,
            ticker="FIXB",
            exchange="XNYS",
            effective_from=date(2026, 2, 1),
            is_primary=True,
        )


@pytest.mark.django_db
def test_company_is_protected_while_listing_history_exists() -> None:
    company = Company.objects.create(legal_name="Fixture Incorporated", display_name="Fixture")
    SecurityListing.objects.create(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
    )

    with pytest.raises(ProtectedError):
        company.delete()


@pytest.mark.django_db
def test_model_timestamps_are_timezone_aware() -> None:
    company = Company.objects.create(legal_name="Fixture Incorporated", display_name="Fixture")
    listing = SecurityListing.objects.create(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
    )

    assert timezone.is_aware(company.created_at)
    assert timezone.is_aware(company.updated_at)
    assert timezone.is_aware(listing.created_at)
    assert timezone.is_aware(listing.updated_at)
