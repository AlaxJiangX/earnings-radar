from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import Q


class WatchlistHardDeleteError(RuntimeError):
    pass


class WatchlistItemQuerySet(models.QuerySet[Any]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise WatchlistHardDeleteError("Watchlist items must be deactivated, not deleted.")


class WatchlistItem(models.Model):
    class PriorityLevel(models.TextChoices):
        NORMAL = "normal", "Normal"
        IMPORTANT = "important", "Important"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="watchlist_items",
    )
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="watchlist_items",
    )
    priority_level = models.CharField(
        max_length=16,
        choices=PriorityLevel.choices,
        default=PriorityLevel.NORMAL,
    )
    alerts_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    objects = WatchlistItemQuerySet.as_manager()

    class Meta:
        ordering = ("user_id", "created_at", "id")
        indexes = [
            models.Index(
                fields=("user", "is_active"),
                name="watchlists_user_active_idx",
            ),
            models.Index(
                fields=("company", "is_active"),
                name="watchlists_company_active_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("user", "company"),
                name="watchlists_item_user_company_unique",
            ),
            models.CheckConstraint(
                condition=Q(priority_level__in=("normal", "important")),
                name="watchlists_item_priority_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(is_active=True) & Q(deactivated_at__isnull=True))
                    | (Q(is_active=False) & Q(deactivated_at__isnull=False))
                ),
                name="watchlists_item_state_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.company_id}:{self.priority_level}"

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise WatchlistHardDeleteError("Watchlist items must be deactivated, not deleted.")
