from __future__ import annotations

import uuid

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField
from django.contrib.postgres.fields.ranges import RangeOperators
from django.db import models
from django.db.models import F, Q

from audit.security import validate_safe_base_url


class Company(models.Model):
    class IssuerType(models.TextChoices):
        DOMESTIC = "domestic", "Domestic"
        FOREIGN_PRIVATE = "foreign_private", "Foreign private issuer"
        OTHER = "other", "Other"
        UNKNOWN = "unknown", "Unknown"

    class MonitoringStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        PENDING_IDENTITY = "pending_identity", "Pending identity"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    legal_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    cik = models.CharField(max_length=10, unique=True, null=True, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    issuer_type = models.CharField(
        max_length=32,
        choices=IssuerType.choices,
        default=IssuerType.UNKNOWN,
    )
    fiscal_year_end_month_day = models.CharField(max_length=5, blank=True)
    investor_relations_url = models.URLField(blank=True, validators=(validate_safe_base_url,))
    monitoring_status = models.CharField(
        max_length=32,
        choices=MonitoringStatus.choices,
        default=MonitoringStatus.PENDING_IDENTITY,
    )
    monitoring_recalculated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("display_name", "id")
        indexes = [
            models.Index(fields=("legal_name",)),
            models.Index(fields=("display_name",)),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(cik__isnull=True) | Q(cik__regex=r"^[0-9]{10}$"),
                name="companies_company_cik_valid",
            ),
            models.CheckConstraint(
                condition=Q(country_code="") | Q(country_code__regex=r"^[A-Z]{2}$"),
                name="companies_company_country_valid",
            ),
            models.CheckConstraint(
                condition=Q(fiscal_year_end_month_day="")
                | Q(fiscal_year_end_month_day__regex=r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"),
                name="companies_company_fiscal_end_valid",
            ),
            models.CheckConstraint(
                condition=Q(legal_name__regex=r"[^[:space:]]")
                & Q(display_name__regex=r"[^[:space:]]"),
                name="companies_company_names_not_blank",
            ),
            models.CheckConstraint(
                condition=Q(issuer_type__in=("domestic", "foreign_private", "other", "unknown")),
                name="companies_company_issuer_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(monitoring_status__in=("active", "inactive", "pending_identity")),
                name="companies_company_monitoring_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name


class SecurityListing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="security_listings",
    )
    ticker = models.CharField(max_length=32)
    exchange = models.CharField(max_length=32)
    security_name = models.CharField(max_length=255, blank=True)
    security_type = models.CharField(max_length=64, default="unknown")
    share_class = models.CharField(max_length=32, blank=True)
    is_primary = models.BooleanField(default=False)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source_evidence = models.ForeignKey(
        "audit.SourceEvidence",
        on_delete=models.PROTECT,
        related_name="security_listings",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("exchange", "ticker", "effective_from", "id")
        indexes = [
            models.Index(fields=("ticker", "exchange")),
            models.Index(fields=("company", "effective_from")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ticker__regex=r"[^[:space:]]")
                & Q(exchange__regex=r"[^[:space:]]")
                & Q(security_type__regex=r"[^[:space:]]"),
                name="companies_listing_identity_not_blank",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")),
                name="companies_listing_date_order_valid",
            ),
            ExclusionConstraint(
                name="companies_listing_exchange_ticker_no_overlap",
                expressions=(
                    ("exchange", RangeOperators.EQUAL),
                    ("ticker", RangeOperators.EQUAL),
                    (
                        models.Func(
                            F("effective_from"),
                            F("effective_to"),
                            models.Value("[)"),
                            function="DATERANGE",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
            ),
            ExclusionConstraint(
                name="companies_listing_primary_no_overlap",
                expressions=(
                    ("company", RangeOperators.EQUAL),
                    (
                        models.Func(
                            F("effective_from"),
                            F("effective_to"),
                            models.Value("[)"),
                            function="DATERANGE",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(is_primary=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.exchange}:{self.ticker}"
