from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from django.utils import timezone

from audit.models import (
    DataSource,
    DomainTargetType,
    RawDataObservation,
    RawDataRecord,
    SyncRun,
)
from audit.services import record_source_evidence
from companies.models import Company
from earnings.models import (
    EarningsEvent,
    FilingEarningsDecision,
    FilingEarningsLink,
)
from filings.models import Filing, FilingDocument
from filings.parsing import PARSER_VERSION


def _token() -> str:
    return uuid.uuid4().hex[:8]


def _accession() -> str:
    return (
        f"{int(uuid.uuid4().hex[:10], 16) % 10**10:010d}-"
        f"26-{int(uuid.uuid4().hex[:6], 16) % 10**6:06d}"
    )


def make_sec_source(suffix: str = "filing") -> DataSource:
    return DataSource.objects.create(
        key=f"sec-fixture-{suffix}-{_token()}",
        name=f"SEC fixture {suffix}",
        source_type=DataSource.SourceType.SEC,
        base_url="https://data.sec.gov",
        is_official=True,
        provider_adapter="sec-edgar",
    )


def make_sec_sync_run(
    *,
    source: DataSource | None = None,
    suffix: str = "sec",
) -> SyncRun:
    source = source or make_sec_source(suffix)
    now = timezone.now()
    return SyncRun.objects.create(
        job_type="fixture.sec-filing",
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.sec-filing:{suffix}:{uuid.uuid4()}",
        started_at=now,
        heartbeat_at=now,
        parser_version=PARSER_VERSION,
        provider_version="sec-edgar-v1",
    )


def make_filing_with_evidence(
    *,
    company: Company,
    form_type: str = "8-K",
    accepted_at: datetime = datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    period_of_report: date | None = None,
    reported_items: str = "",
    document_types: Sequence[str] = (),
    suffix: str = "filing",
) -> Filing:
    source = make_sec_source(suffix)
    run = make_sec_sync_run(source=source, suffix=suffix)
    now = timezone.now()
    payload = f'{{"fixture":"{suffix}","token":"{_token()}"}}'.encode()
    raw = RawDataRecord.objects.create(
        source=source,
        first_sync_run=run,
        source_url=f"https://data.sec.gov/submissions/CIK{company.cik}.json",
        request_fingerprint=hashlib.sha256(f"{suffix}:{uuid.uuid4()}".encode()).hexdigest(),
        fetched_at=now,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )
    RawDataObservation.objects.create(
        sync_run=run,
        raw_data_record=raw,
        observed_at=now,
    )
    accession = _accession()
    archive_root = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(company.cik or '0')}/{accession.replace('-', '')}/"
    )
    filing = Filing.objects.create(
        company=company,
        accession_number=accession,
        form_type=form_type,
        accepted_at=accepted_at,
        period_of_report=period_of_report,
        primary_document="main.htm",
        filing_url=archive_root + "main.htm",
        reported_items=reported_items,
    )
    evidence = record_source_evidence(
        raw_data_record=raw,
        sync_run=run,
        target_type=DomainTargetType.FILING,
        target_id=filing.pk,
        field_name="",
        raw_value={"accession_number": accession, "position": 1},
        normalized_value={
            "accession_number": accession,
            "form_type": form_type,
            "accepted_at": accepted_at.isoformat(),
            "period_of_report": period_of_report.isoformat() if period_of_report else None,
            "reported_items": reported_items,
        },
        confidence=Decimal("1"),
        normalizer_version=PARSER_VERSION,
    ).evidence
    filing.source_evidence = evidence
    filing.save(update_fields=("source_evidence", "updated_at"))
    for index, document_type in enumerate(document_types):
        filename = f"exhibit-{index}.htm"
        FilingDocument.objects.create(
            filing=filing,
            filename=filename,
            document_type=document_type,
            url=archive_root + filename,
        )
    return filing


def add_raw_observation_for_run(*, filing: Filing, sync_run: SyncRun) -> None:
    evidence = filing.source_evidence
    assert evidence is not None
    RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=evidence.raw_data_record,
        observed_at=timezone.now(),
    )


def make_filing_link_decision(
    *,
    filing: Filing,
    relation_type: str = "RELEASE_FILING",
    **overrides: object,
) -> FilingEarningsDecision:
    assert filing.source_evidence is not None
    values: dict[str, object] = {
        "filing": filing,
        "relation_type": relation_type,
        "target_event": None,
        "decision_type": "review_required",
        "status": "open",
        "classification": None,
        "confidence": None,
        "match_rule_version": "fixture-match-v1",
        "classification_rule_version": "",
        "decision_source": "automatic",
        "match_factors": {},
        "reason": "",
        "source_raw_data_record": None,
        "source_evidence": None,
        "actor_user": None,
        "sync_run": make_sec_sync_run(
            source=filing.source_evidence.sync_run.source, suffix="decision"
        ),
        "request_id": "",
        "decided_at": timezone.now(),
        "supersedes": None,
        "decision_key": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        **overrides,
    }
    return FilingEarningsDecision.objects.create(**values)


def make_filing_earnings_link(
    *,
    filing: Filing,
    earnings_event: EarningsEvent,
    current_decision: FilingEarningsDecision,
    **overrides: object,
) -> FilingEarningsLink:
    values: dict[str, object] = {
        "filing": filing,
        "earnings_event": earnings_event,
        "relation_type": "RELEASE_FILING",
        "release_filing_classification": "YES",
        "classification_reason": "ITEM_202_WITH_EARNINGS_EXHIBIT",
        "classification_rule_version": "fixture-classification-v1",
        "match_rule_version": "fixture-match-v1",
        "confidence": "BOUNDED_WINDOW",
        "review_status": "auto",
        "review_reason": "",
        "source_evidence": filing.source_evidence,
        "current_decision": current_decision,
        "reviewed_by": None,
        "reviewed_at": None,
        **overrides,
    }
    return FilingEarningsLink.objects.create(**values)
