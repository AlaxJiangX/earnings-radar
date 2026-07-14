from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

ALLOWED_CODES = frozenset({"SP500", "NASDAQ100", "DJIA", "RUSSELL2000"})
ALLOWED_INDEX_GROUPS = frozenset({"LARGE", "SMALL"})


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
