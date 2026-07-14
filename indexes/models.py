from __future__ import annotations

import uuid

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField
from django.contrib.postgres.fields.ranges import RangeOperators
from django.db import models
from django.db.models import F, Q

ALLOWED_CODES = frozenset({"SP500", "NASDAQ100", "DJIA", "RUSSELL2000"})
ALLOWED_INDEX_GROUPS = frozenset({"LARGE", "SMALL"})
NORMATIVE_MEMBERSHIP_STATUSES = ("announced", "active", "ended")
ALLOWED_MEMBERSHIP_STATUSES = NORMATIVE_MEMBERSHIP_STATUSES + ("cancelled", "corrected")


class MarketIndex(models.Model):
    class IndexGroup(models.TextChoices):
        LARGE = "LARGE", "Large-cap index"
        SMALL = "SMALL", "Small-cap index"

    class Code(models.TextChoices):
        SP500 = "SP500", "S&P 500"
        NASDAQ100 = "NASDAQ100", "Nasdaq 100"
        DJIA = "DJIA", "Dow Jones Industrial Average"
        RUSSELL2000 = "RUSSELL2000", "Russell 2000"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    index_group = models.CharField(max_length=16, choices=IndexGroup.choices)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        indexes = [
            models.Index(fields=("index_group",)),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(code__in=ALLOWED_CODES) & Q(code__regex=r"[^[:space:]]")),
                name="indexes_market_index_code_valid",
            ),
            models.CheckConstraint(
                condition=Q(name__regex=r"[^[:space:]]"),
                name="indexes_market_index_name_not_blank",
            ),
            models.CheckConstraint(
                condition=Q(index_group__in=ALLOWED_INDEX_GROUPS),
                name="indexes_market_index_group_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(code="SP500", index_group="LARGE")
                    | Q(code="NASDAQ100", index_group="LARGE")
                    | Q(code="DJIA", index_group="LARGE")
                    | Q(code="RUSSELL2000", index_group="SMALL")
                ),
                name="indexes_market_index_code_group_consistent",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class IndexMembership(models.Model):
    class Status(models.TextChoices):
        ANNOUNCED = "announced", "Announced"
        ACTIVE = "active", "Active"
        ENDED = "ended", "Ended"
        CANCELLED = "cancelled", "Cancelled"
        CORRECTED = "corrected", "Corrected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    index = models.ForeignKey(
        MarketIndex,
        on_delete=models.PROTECT,
        related_name="memberships",
    )

    security_listing = models.ForeignKey(
        "companies.SecurityListing",
        on_delete=models.PROTECT,
        related_name="index_memberships",
    )

    status = models.CharField(max_length=16, choices=Status.choices)

    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    announcement_date = models.DateField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
    )

    source_evidence = models.ForeignKey(
        "audit.SourceEvidence",
        on_delete=models.PROTECT,
        related_name="index_memberships",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("security_listing", "index", "effective_from", "id")
        indexes = [
            models.Index(fields=("index", "effective_from")),
            models.Index(fields=("security_listing", "effective_from")),
            models.Index(fields=("status", "effective_from")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=ALLOWED_MEMBERSHIP_STATUSES),
                name="indexes_membership_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")),
                name="indexes_membership_date_order_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status="ended") | Q(effective_to__isnull=False),
                name="indexes_membership_ended_has_end_date",
            ),
            models.CheckConstraint(
                condition=(
                    Q(announcement_date__isnull=True)
                    | Q(announcement_date__lte=F("effective_from"))
                ),
                name="indexes_membership_announcement_not_after_start",
            ),
            models.UniqueConstraint(
                fields=("index", "security_listing", "effective_from"),
                condition=Q(status__in=NORMATIVE_MEMBERSHIP_STATUSES),
                name="indexes_membership_normative_identity_unique",
            ),
            ExclusionConstraint(
                name="indexes_membership_normative_no_overlap",
                expressions=(
                    ("security_listing", RangeOperators.EQUAL),
                    ("index", RangeOperators.EQUAL),
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
                condition=Q(status__in=NORMATIVE_MEMBERSHIP_STATUSES),
            ),
        ]

    def __str__(self) -> str:
        index_code = self.index.code if self.index_id else "?"
        listing_ticker = self.security_listing.ticker if self.security_listing_id else "?"
        return f"{index_code}:{listing_ticker} [{self.status}]"
