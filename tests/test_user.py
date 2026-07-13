import pytest

from accounts.models import User


@pytest.mark.django_db
def test_user_uses_email_as_login_identifier() -> None:
    user = User.objects.create_user(
        email="person@example.com",
        password="a-test-password-only",
    )

    assert user.email == "person@example.com"
    assert user.username is None
    assert user.check_password("a-test-password-only")
