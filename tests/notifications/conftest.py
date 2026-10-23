from __future__ import annotations

from collections.abc import Iterator

import pytest

from accounts.models import User


@pytest.fixture
def user(db: object) -> Iterator[User]:
    del db
    obj = User.objects.create_user(
        email="notifications-fixture@example.com",
        password="fixture-password-only",
    )
    yield obj
