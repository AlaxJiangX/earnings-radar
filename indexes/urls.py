from __future__ import annotations

from django.urls import path

from indexes.views import index_changes

app_name = "indexes"

urlpatterns = [
    path("index-changes/", index_changes, name="index_changes"),
]
