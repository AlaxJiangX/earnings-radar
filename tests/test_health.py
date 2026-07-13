import pytest
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
def test_health_check_reports_database_ready(client: Client) -> None:
    response = client.get(reverse("health"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_check_rejects_post(client: Client) -> None:
    response = client.post(reverse("health"))

    assert response.status_code == 405
