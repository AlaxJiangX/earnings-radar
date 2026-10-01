from __future__ import annotations

from django.urls import path

from companies.views import company_detail, company_list

app_name = "companies"

urlpatterns = [
    path("companies/", company_list, name="list"),
    path("companies/<str:ticker>/", company_detail, name="detail"),
]
