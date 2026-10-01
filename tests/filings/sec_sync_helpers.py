"""Shared SEC sync fixtures for Stage 4.5A-I2 orchestration tests."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from audit.models import DataSource
from companies.models import Company, SecurityListing
from earnings.models import MonitoringPoolSnapshot
from earnings.services import EARNINGS_MONITORING_POOL_SELECTOR_VERSION, select_monitoring_pool
from indexes.models import IndexMembership, MarketIndex
from providers.http import TransportRequest, TransportResponse

AS_OF = date(2026, 9, 30)
_INDEX_URL_RE = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d+)/index\.json$")


@dataclass(frozen=True, slots=True)
class FilingSpec:
    form: str = "10-Q"
    period_of_report: str = ""
    items: str = ""
    exhibit_type: str | None = None
    acceptance_datetime: str = "2026-03-09T16:30:00"


def make_company(*, cik: str) -> Company:
    token = uuid.uuid4().hex[:8]
    return Company.objects.create(
        legal_name=f"SEC I2 {token}",
        display_name=f"SEC I2 {token}",
        cik=cik,
    )


def make_sec_source(*, key: str | None = None) -> DataSource:
    return DataSource.objects.create(
        key=key or f"sec-i2-{uuid.uuid4().hex[:8]}",
        name="SEC I2",
        source_type=DataSource.SourceType.SEC,
        base_url="https://data.sec.gov",
        is_official=True,
        provider_adapter="sec-edgar",
    )


def make_pool(*, companies: Sequence[Company]) -> MonitoringPoolSnapshot:
    index, _ = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
    )
    for company in companies:
        listing = SecurityListing.objects.create(
            company=company,
            ticker=f"I2{uuid.uuid4().hex[:5].upper()}",
            exchange="NYSE",
            security_name="SEC I2 common",
            security_type="common_stock",
            effective_from=date(2026, 1, 1),
        )
        IndexMembership.objects.create(
            index=index,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
    return select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot


class SecFilingTransport:
    """Serve deterministic submissions and filing-directory payloads per CIK."""

    def __init__(self, specs: FilingSpec | Mapping[str, FilingSpec]) -> None:
        self.specs = specs
        self.requests: list[TransportRequest] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        index_match = _INDEX_URL_RE.fullmatch(request.url)
        if index_match is None:
            cik = request.url.rsplit("/CIK", 1)[1][:10]
            body = self._submissions_body(cik=cik)
        else:
            body = self._directory_body(
                cik=index_match.group(1).zfill(10),
                accession_digits=index_match.group(2),
            )
        return TransportResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=body,
            fetched_at=datetime.now(UTC),
        )

    def _spec(self, cik: str) -> FilingSpec:
        if isinstance(self.specs, FilingSpec):
            return self.specs
        try:
            return self.specs[cik]
        except KeyError:
            raise AssertionError(f"No fixture spec for CIK {cik}.") from None

    def _submissions_body(self, *, cik: str) -> bytes:
        spec = self._spec(cik)
        columns: dict[str, list[str]] = {
            "accessionNumber": [f"{cik}-26-000001"],
            "form": [spec.form],
            "acceptanceDateTime": [spec.acceptance_datetime],
            "reportDate": [spec.period_of_report],
            "primaryDocument": ["quarter.htm"],
        }
        if spec.items:
            columns["items"] = [spec.items]
        return json.dumps({"cik": int(cik), "filings": {"recent": columns}}).encode()

    def _directory_body(self, *, cik: str, accession_digits: str) -> bytes:
        spec = self._spec(cik)
        directory = f"/Archives/edgar/data/{int(cik)}/{accession_digits}"
        items: list[dict[str, str]] = [{"name": "quarter.htm", "type": "text/html"}]
        if spec.exhibit_type is not None:
            items.append({"name": "exhibit.htm", "type": spec.exhibit_type})
        return json.dumps({"directory": {"name": directory, "item": items}}).encode()
