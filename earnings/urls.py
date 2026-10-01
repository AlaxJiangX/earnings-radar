from __future__ import annotations

from django.urls import path

from earnings.views import earnings_list

app_name = "earnings"

urlpatterns = [
    path("earnings/", earnings_list, name="list"),
]
