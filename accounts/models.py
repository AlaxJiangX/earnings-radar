import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models

from accounts.managers import UserManager


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None  # type: ignore[assignment]
    email = models.EmailField(unique=True)
    timezone = models.CharField(  # noqa: DJ001
        max_length=64,
        null=True,
        blank=True,
        help_text="IANA timezone for display and reminder windows (data-model 3.1).",
    )

    USERNAME_FIELD: ClassVar[str] = "email"  # type: ignore[misc]
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects: ClassVar[UserManager] = UserManager()  # type: ignore[assignment]

    def __str__(self) -> str:
        return self.email
