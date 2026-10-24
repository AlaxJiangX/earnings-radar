from django.contrib import admin
from django.urls import include, path

from config.health import health_check
from config.pages import (
    dashboard,
    notification_read,
    notification_settings,
    notifications_page,
    watchlist_add,
    watchlist_alerts,
    watchlist_page,
    watchlist_priority,
    watchlist_remove,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    path("", include("indexes.urls")),
    path("", include("earnings.urls")),
    path("", include("companies.urls")),
    path("dashboard/", dashboard, name="dashboard"),
    path("watchlist/", watchlist_page, name="watchlist"),
    path("watchlist/add/", watchlist_add, name="watchlist_add"),
    path("watchlist/remove/", watchlist_remove, name="watchlist_remove"),
    path("watchlist/priority/", watchlist_priority, name="watchlist_priority"),
    path("watchlist/alerts/", watchlist_alerts, name="watchlist_alerts"),
    path("notifications/", notifications_page, name="notifications"),
    path("notifications/read/", notification_read, name="notification_read"),
    path(
        "settings/notifications/",
        notification_settings,
        name="notification_settings",
    ),
]
