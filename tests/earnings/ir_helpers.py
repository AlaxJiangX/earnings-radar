"""Shared fixture builders for Stage 4.5B IR confirmation tests."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from audit.models import DataSource, SyncRun
from companies.models import Company
from earnings.ir_parsing import (
    FIXTURE_IR_FORMAT_VERSION,
    FIXTURE_IR_PARSER_VERSION,
    FIXTURE_IR_PROVIDER_KEY,
    FIXTURE_IR_PROVIDER_VERSION,
    FixtureInvestorRelationsParser,
)
from earnings.services.ir_ingestion import (
    InvestorRelationsIngestionResult,
    ingest_investor_relations_payload,
)
from earnings.services.ir_sync import start_investor_relations_sync_run
from tests.earnings.helpers import make_company

IR_FETCHED_AT = datetime(2026, 10, 1, 12, 0, 1, tzinfo=UTC)
DEFAULT_IR_SOURCE_KEY = "fixture-ir-source"
DEFAULT_IR_SOURCE_URL_BASE = "https://ir.example.test"


def make_ir_company(suffix: str = "ir") -> Company:
    company = make_company(suffix)
    company.investor_relations_url = f"{DEFAULT_IR_SOURCE_URL_BASE}/{suffix}/{company.pk}"
    company.save(update_fields=("investor_relations_url",))
    return company


def make_ir_source(suffix: str = "ir") -> DataSource:
    token = uuid.uuid4().hex[:8]
    return DataSource.objects.create(
        key=f"fixture-ir-{suffix}-{token}",
        name=f"Fixture IR {suffix}",
        source_type=DataSource.SourceType.INVESTOR_RELATIONS,
        base_url="https://ir.example.test/",
        provider_adapter=FIXTURE_IR_PROVIDER_KEY,
        is_official=True,
        license_notes="Synthetic test-only IR source; live ingestion remains BLOCKED.",
    )


def make_ir_sync_run(
    *,
    company: Company,
    source: DataSource,
    source_keys: tuple[str, ...] = (DEFAULT_IR_SOURCE_KEY,),
    request_id: str = "fixture-ir-request",
) -> SyncRun:
    result = start_investor_relations_sync_run(
        source=source,
        provider_key=FIXTURE_IR_PROVIDER_KEY,
        company_ids=[company.pk],
        source_keys=list(source_keys),
        request_id=request_id,
        provider_version=FIXTURE_IR_PROVIDER_VERSION,
        parser_version=FIXTURE_IR_PARSER_VERSION,
    )
    return result.sync_run


def ir_item(
    company: Company,
    *,
    item_type: str = "release_confirmation",
    period_end_date: str = "2026-09-30",
    period_type: str = "Q3",
    source_event_identity: str | None = None,
    **facts: Any,
) -> dict[str, object]:
    item: dict[str, object] = {
        "company_id": str(company.pk),
        "period_end_date": period_end_date,
        "period_type": period_type,
        "item_type": item_type,
    }
    if source_event_identity is not None:
        item["source_event_identity"] = source_event_identity
    item.update(facts)
    return item


def ir_payload(
    items: list[dict[str, object]],
    *,
    source_key: str = DEFAULT_IR_SOURCE_KEY,
    provider_key: str = FIXTURE_IR_PROVIDER_KEY,
    provider_version: str = FIXTURE_IR_PROVIDER_VERSION,
) -> bytes:
    return json.dumps(
        {
            "fixture_version": FIXTURE_IR_FORMAT_VERSION,
            "provider_key": provider_key,
            "provider_version": provider_version,
            "source_key": source_key,
            "items": items,
        }
    ).encode()


def ingest_ir(
    *,
    sync_run: SyncRun,
    payload: bytes,
    source_key: str = DEFAULT_IR_SOURCE_KEY,
    company: Company,
    fetched_at: datetime = IR_FETCHED_AT,
) -> InvestorRelationsIngestionResult:
    return ingest_investor_relations_payload(
        sync_run=sync_run,
        parser=FixtureInvestorRelationsParser(),
        raw_content=payload,
        source_key=source_key,
        provider_key=FIXTURE_IR_PROVIDER_KEY,
        provider_version=FIXTURE_IR_PROVIDER_VERSION,
        source_url=f"{DEFAULT_IR_SOURCE_URL_BASE}/{source_key}/{company.pk}",
        fetched_at=fetched_at,
        request_identity={"company_id": str(company.pk), "source_key": source_key},
    )
