from collections.abc import Iterator

import pytest

from accounts.models import User


@pytest.fixture
def user(db: object) -> Iterator[User]:
    del db
    obj = User.objects.create_user(
        email="index-actor@example.com",
        password="fixture-password-only",
        is_staff=True,
    )
    yield obj
