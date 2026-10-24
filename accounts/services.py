"""Public write services for the accounts app."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction

if TYPE_CHECKING:
    from accounts.models import User


class AccountServiceError(ValueError):
    pass


def set_user_timezone(user: User, timezone_value: str | None) -> None:
    """Set or clear the user's display timezone (data-model section 3.1).

    The value must be a resolvable IANA zone name; an empty / ``None``
    value clears the field (display falls back to browser-local time).
    Repeated calls with the same value are idempotent no-ops.
    """

    from accounts.models import User as UserModel

    normalized = (timezone_value or "").strip()
    if normalized:
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise AccountServiceError(f"Unknown IANA timezone: {normalized}.") from error
    with transaction.atomic():
        instance = UserModel.objects.select_for_update().get(pk=user.pk)
        new_value = normalized or None
        if instance.timezone == new_value:
            return
        instance.timezone = new_value
        instance.save(update_fields=("timezone",))
