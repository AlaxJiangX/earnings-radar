"""Public SEC EDGAR metadata provider; database writes belong to filings services."""

from __future__ import annotations

import re
from datetime import datetime

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.http import HttpClientConfig, HttpTransport, ProviderHttpClient, RetryPolicy
from providers.sec_http import SecHttpsTransport
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

SEC_PROVIDER_KEY = "sec-edgar"
SEC_PROVIDER_VERSION = "sec-submissions-v1"
_CIK_RE = re.compile(r"^[0-9]{10}$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CONTACT_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


def submissions_request(*, cik: str, started_at: datetime) -> ProviderRequest:
    if not _CIK_RE.fullmatch(cik):
        raise ProviderValidationError("SEC CIK must be ten ASCII digits.")
    return ProviderRequest(
        capability=ProviderCapability.SEC_EDGAR,
        scope={"kind": "submissions", "cik": cik},
        request_started_at=started_at,
        source_url=f"https://data.sec.gov/submissions/CIK{cik}.json",
        request_identity={"cik": cik},
    )


def filing_index_request(
    *, cik: str, accession_number: str, started_at: datetime
) -> ProviderRequest:
    if not _CIK_RE.fullmatch(cik) or not _ACCESSION_RE.fullmatch(accession_number):
        raise ProviderValidationError("SEC filing index identity is invalid.")
    accession_path = accession_number.replace("-", "")
    return ProviderRequest(
        capability=ProviderCapability.SEC_EDGAR,
        scope={"kind": "filing_index", "cik": cik, "accession_number": accession_number},
        request_started_at=started_at,
        source_url=(
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/index.json"
        ),
        request_identity={"cik": cik, "accession_number": accession_number},
    )


class SecEdgarProvider(Provider):
    provider_key = SEC_PROVIDER_KEY
    provider_version = SEC_PROVIDER_VERSION
    capabilities = frozenset({ProviderCapability.SEC_EDGAR})

    def __init__(
        self,
        *,
        user_agent: str,
        max_requests_per_second: int = 4,
        transport: HttpTransport | None = None,
    ) -> None:
        contact = _CONTACT_RE.search(user_agent)
        application_name = (
            user_agent[: contact.start()] + user_agent[contact.end() :] if contact else ""
        )
        if not contact or not re.search(r"[A-Za-z0-9]", application_name):
            raise ProviderValidationError(
                "SEC_USER_AGENT must identify the application and include a contact email."
            )
        if isinstance(max_requests_per_second, bool) or not 1 <= max_requests_per_second <= 9:
            raise ProviderValidationError("SEC rate must be between 1 and 9 requests per second.")
        self._http = ProviderHttpClient(
            transport=transport
            or SecHttpsTransport(max_requests_per_second=max_requests_per_second),
            config=HttpClientConfig(
                user_agent=user_agent,
                forbidden_as_rate_limit=True,
                retry_policy=RetryPolicy(
                    max_attempts=2, base_delay_seconds=2, max_delay_seconds=30
                ),
            ),
        )

    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        scope = dict(request.scope)
        kind = scope.get("kind")
        cik = scope.get("cik")
        if not isinstance(cik, str):
            raise ProviderValidationError("SEC request scope requires CIK.")
        if kind == "submissions":
            expected = submissions_request(cik=cik, started_at=request.request_started_at)
        elif kind == "filing_index":
            accession = scope.get("accession_number")
            if not isinstance(accession, str):
                raise ProviderValidationError("SEC filing index requires accession number.")
            expected = filing_index_request(
                cik=cik, accession_number=accession, started_at=request.request_started_at
            )
        else:
            raise ProviderValidationError("Unsupported SEC request kind.")
        if (
            request.source_url != expected.source_url
            or request.method != "GET"
            or scope != expected.scope
            or request.request_identity != expected.request_identity
        ):
            raise ProviderValidationError("SEC request does not match its canonical endpoint.")
        return self._http.fetch(
            provider_key=self.provider_key,
            provider_version=self.provider_version,
            request=request,
            headers={"Accept": "application/json"},
        )
