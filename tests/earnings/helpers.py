from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal

from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from audit.services import DataChangeWriteResult, record_data_change, record_source_evidence
from companies.models import Company
from earnings.identity import IDENTITY_RULE_VERSION, derive_earnings_identity_key
from earnings.models import EarningsCalendarObservation, EarningsEvent


def make_company(suffix: str, *, display_name: str | None = None) -> Company:
    token = uuid.uuid4().hex[:8]
    return Company.objects.create(
        cik=str(100000 + int(token, 16) % 899999).zfill(10),
        legal_name=f"Legal {suffix} {token}",
        display_name=display_name or f"Company {suffix} {token}",
    )


def make_event(
    *,
    company: Company | None = None,
    period_end_date: date = date(2026, 3, 31),
    period_type: str = "Q1",
    **overrides: object,
) -> EarningsEvent:
    company = company or make_company("event")
    identity_key = derive_earnings_identity_key(
        company_id=company.pk,
        period_end_date=period_end_date,
        period_type=period_type,
    )
    values = {
        "company": company,
        "period_end_date": period_end_date,
        "period_type": period_type,
        "identity_status": "canonical",
        "identity_key": identity_key,
        "identity_rule_version": IDENTITY_RULE_VERSION,
        "includes_q4": period_type == "FY",
        **overrides,
    }
    return EarningsEvent.objects.create(**values)


def make_sync_run(suffix: str = "fixture") -> SyncRun:
    source = DataSource.objects.create(
        key=f"earnings-date-change-{suffix}-{uuid.uuid4().hex[:8]}",
        name=f"Earnings date change {suffix} fixture",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://example.test/earnings",
        license_notes="Synthetic test-only source.",
    )
    now = timezone.now()
    return SyncRun.objects.create(
        job_type="fixture.earnings-date-change",
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.earnings-date-change:{suffix}:{uuid.uuid4()}",
        started_at=now,
        heartbeat_at=now,
    )


def make_source_evidence(
    *,
    event: EarningsEvent,
    field_name: str,
    normalized_value: object,
    suffix: str = "evidence",
) -> tuple[SyncRun, SourceEvidence]:
    sync_run = make_sync_run(suffix)
    payload = f'{{"field":"{field_name}","value":{normalized_value!r}}}'.encode()
    raw_record = RawDataRecord.objects.create(
        source=sync_run.source,
        first_sync_run=sync_run,
        source_url=f"https://example.test/earnings/{field_name}",
        request_fingerprint=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        fetched_at=timezone.now(),
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )
    RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=raw_record,
        observed_at=timezone.now(),
    )
    result = record_source_evidence(
        raw_data_record=raw_record,
        sync_run=sync_run,
        target_type="earnings_event",
        target_id=event.pk,
        field_name=field_name,
        raw_value=normalized_value,
        normalized_value=normalized_value,
        confidence=Decimal("0.9000"),
        normalizer_version="fixture-v1",
    )
    return sync_run, result.evidence


def make_data_change(
    *,
    event: EarningsEvent,
    field_name: str = "estimated_release",
    old_value: object = None,
    new_value: object = None,
    suffix: str = "data-change",
) -> DataChangeWriteResult:
    if new_value is None:
        new_value = {
            "kind": "date",
            "precision": "date_only",
            "value": "2026-10-24",
        }
    sync_run = make_sync_run(suffix)
    return record_data_change(
        target_type="earnings_event",
        target_id=event.pk,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        rule_version="earnings-date-change-v1",
        sync_run=sync_run,
    )


def make_calendar_source(suffix: str = "calendar") -> DataSource:
    token = uuid.uuid4().hex[:8]
    return DataSource.objects.create(
        key=f"fixture-earnings-calendar-{suffix}-{token}",
        name=f"Fixture earnings calendar {suffix}",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://calendar.example.test/",
        provider_adapter="fixture-calendar-provider",
        license_notes="Synthetic test-only source.",
    )


def make_calendar_raw_record(
    source: DataSource,
    suffix: str = "calendar",
) -> RawDataRecord:
    now = timezone.now()
    sync_run = SyncRun.objects.create(
        job_type="fixture.earnings-calendar",
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.earnings-calendar:{suffix}:{uuid.uuid4()}",
        started_at=now,
        heartbeat_at=now,
    )
    payload = b'{"fixture":"calendar"}'
    return RawDataRecord.objects.create(
        source=source,
        first_sync_run=sync_run,
        source_url=f"https://calendar.example.test/{suffix}",
        request_fingerprint=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        fetched_at=now,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )


def make_calendar_observation(
    *,
    source: DataSource | None = None,
    raw_data_record: RawDataRecord | None = None,
    **overrides: object,
) -> EarningsCalendarObservation:
    source = source or make_calendar_source()
    raw_data_record = raw_data_record or make_calendar_raw_record(source)
    values: dict[str, object] = {
        "source": source,
        "raw_data_record": raw_data_record,
        "provider_key": source.provider_adapter,
        "provider_version": "fixture-provider-v1",
        "parser_version": "fixture-parser-v1",
        "provider_event_id": f"evt-{uuid.uuid4().hex[:8]}",
        "raw_position": 1,
        "cik": "",
        "ticker": "FAKE",
        "exchange": "NASDAQ",
        "provider_symbol": "FAKE",
        "company_name": "Fixture Calendar Corp",
        "fiscal_label_raw": "Q1",
        "fiscal_year": 2026,
        "period_end_date": date(2026, 3, 31),
        "period_type": "Q1",
        "fiscal_calendar_type": "month_based",
        "period_length_weeks": None,
        "estimated_release_date": date(2026, 4, 22),
        "estimated_release_at": None,
        "estimated_release_precision": "date_only",
        "release_session": "after_market",
        "source_observed_at": None,
        "confidence": Decimal("0.9000"),
        **overrides,
    }
    return EarningsCalendarObservation.objects.create(**values)
