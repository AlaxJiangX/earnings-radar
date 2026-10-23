"""Read-only Filing selectors for cross-module page composition."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from django.db.models import QuerySet

from filings.models import Filing


def recent_filings_for_companies(
    *,
    company_ids: Sequence[UUID],
    limit: int,
) -> QuerySet[Filing]:
    """Return the most recently accepted SEC filings for the given Companies."""

    return (
        Filing.objects.filter(company_id__in=company_ids)
        .select_related("company")
        .order_by("-accepted_at", "id")[:limit]
    )
