from __future__ import annotations

import json

import pytest

from audit.models import DataChange, RawDataObservation, RawDataRecord
from filings.models import Filing
from filings.parsing import PARSER_VERSION, parse_submissions
from tests.filings.test_sec_provider_and_parsing import ACCESSION, CIK
from tests.filings.test_sec_sync import FixtureTransport, _run, _setup


def submissions_payload(
    *,
    items: object = None,
    form: str = "10-Q",
    acceptance: str = "2026-03-09T16:30:00",
    report_date: str = "",
) -> bytes:
    recent: dict[str, object] = {
        "accessionNumber": [ACCESSION, "0000001234-26-000002"],
        "form": [form, "4"],
        "acceptanceDateTime": [acceptance, "not-a-date"],
        "reportDate": [report_date, ""],
        "primaryDocument": ["quarter.htm", "ignored.htm"],
    }
    if items is not None:
        recent["items"] = items
    return json.dumps({"cik": 1234, "filings": {"recent": recent}}).encode()


def test_parser_version_is_v2() -> None:
    assert PARSER_VERSION == "sec-filings-v2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("2.02", "2.02"),
        ("2.02,9.01", "2.02,9.01"),
        ("9.01,2.02,2.02", "2.02,9.01"),
        (" 2.02 , 9.01 ", "2.02,9.01"),
        ("10.01,2.02,1.01", "1.01,2.02,10.01"),
        ("02.02", "2.02"),
        ("", ""),
        (" , ", ""),
        ("2.02,bad", ""),
        ("2.2", ""),
        ("-", ""),
        (None, ""),
        (123, ""),
        (["2.02"], ""),
    ),
)
def test_normalizes_single_row_items(raw: object, expected: str) -> None:
    rows = parse_submissions(submissions_payload(items=[raw, ""]), cik=CIK)
    assert rows[0].reported_items == expected


def test_missing_items_column_is_treated_as_unavailable() -> None:
    rows = parse_submissions(submissions_payload(), cik=CIK)
    assert rows[0].reported_items == ""


def test_row_length_mismatch_is_treated_as_unavailable() -> None:
    rows = parse_submissions(
        submissions_payload(items=["2.02", "9.01", "1.01"]),
        cik=CIK,
    )
    assert [row.reported_items for row in rows] == [""]


def test_malformed_row_does_not_fail_other_rows() -> None:
    payload = submissions_payload(items=["2.02", "not-an-item"])
    recent = json.loads(payload)["filings"]["recent"]
    recent["accessionNumber"] = [ACCESSION, "0000001234-26-000003"]
    recent["form"] = ["10-Q", "10-K"]
    recent["acceptanceDateTime"] = ["2026-03-09T16:30:00", "2026-03-10T16:30:00"]
    recent["primaryDocument"] = ["quarter.htm", "annual.htm"]
    rows = parse_submissions(
        json.dumps({"cik": 1234, "filings": {"recent": recent}}).encode(),
        cik=CIK,
    )
    assert [row.reported_items for row in rows] == ["2.02", ""]


def test_existing_stage_4_4_fixture_without_items_still_parses() -> None:
    rows = parse_submissions(submissions_payload(form="10-Q"), cik=CIK)
    assert len(rows) == 1
    assert rows[0].accession_number == ACCESSION
    assert rows[0].reported_items == ""


@pytest.mark.django_db(transaction=True)
def test_reported_items_persist_with_parser_v2_and_replay_without_duplicates() -> None:
    _, source, snapshot = _setup()
    transport = FixtureTransport(
        submissions_payload=submissions_payload(form="8-K", items=["2.02,9.01", ""])
    )
    first = _run(source=source, snapshot=snapshot, transport=transport, key="items-first")
    assert first.sync_run.status == "succeeded"
    filing = Filing.objects.get()
    assert filing.reported_items == "2.02,9.01"
    evidence = filing.source_evidence
    assert evidence is not None
    assert evidence.raw_data_record.parser_version == "sec-filings-v2"
    assert RawDataObservation.objects.filter(sync_run=first.sync_run).count() == 2

    second = _run(source=source, snapshot=snapshot, transport=transport, key="items-second")
    assert second.sync_run.status == "succeeded"
    assert Filing.objects.count() == 1
    assert Filing.objects.get().reported_items == "2.02,9.01"
    assert RawDataRecord.objects.count() == 2
    assert DataChange.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_historical_empty_reported_items_is_not_silently_backfilled() -> None:
    _, source, snapshot = _setup()
    legacy = FixtureTransport(submissions_payload=submissions_payload())
    legacy_run = _run(source=source, snapshot=snapshot, transport=legacy, key="legacy")
    assert legacy_run.sync_run.status == "succeeded"
    assert Filing.objects.get().reported_items == ""

    upgraded = FixtureTransport(submissions_payload=submissions_payload(items=["2.02", "9.01"]))
    upgraded_run = _run(source=source, snapshot=snapshot, transport=upgraded, key="upgraded")
    assert upgraded_run.sync_run.status == "succeeded"
    assert upgraded_run.sync_run.skipped_count == 1
    assert Filing.objects.get().reported_items == ""
    assert DataChange.objects.count() == 0
