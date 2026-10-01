"""Strict, fixture-testable SEC metadata normalization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from filings.models import TARGET_FORMS

_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_EASTERN = ZoneInfo("America/New_York")
PARSER_VERSION = "sec-filings-v1"


class SecMetadataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FilingMetadata:
    accession_number: str
    form_type: str
    accepted_at: datetime
    period_of_report: date | None
    primary_document: str
    filing_url: str
    raw_position: int


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    filename: str
    document_type: str
    url: str
    description: str = ""


def normalize_accession_number(value: object) -> str:
    if not isinstance(value, str):
        raise SecMetadataError("SEC accession number must be text.")
    normalized = value.strip()
    if not _ACCESSION_RE.fullmatch(normalized):
        raise SecMetadataError("SEC accession number is malformed.")
    return normalized


def _filename(value: object) -> str:
    if not isinstance(value, str) or not _FILENAME_RE.fullmatch(value) or ".." in value:
        raise SecMetadataError("SEC document filename is unsafe or malformed.")
    return value


def _object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        raise SecMetadataError("SEC metadata is not valid JSON.") from None
    if not isinstance(value, dict):
        raise SecMetadataError("SEC metadata root must be an object.")
    return value


def parse_submissions(payload: bytes, *, cik: str) -> tuple[FilingMetadata, ...]:
    data = _object(payload)
    actual_cik = data.get("cik")
    if isinstance(actual_cik, bool) or str(actual_cik).zfill(10) != cik:
        raise SecMetadataError("SEC submissions CIK does not match the requested Company.")
    filings = data.get("filings")
    if not isinstance(filings, dict):
        raise SecMetadataError("SEC submissions filing section is missing.")
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        raise SecMetadataError("SEC submissions recent section is missing.")
    fields = ("accessionNumber", "form", "acceptanceDateTime", "reportDate", "primaryDocument")
    columns: dict[str, list[object]] = {}
    for field in fields:
        column = recent.get(field)
        if not isinstance(column, list):
            raise SecMetadataError("SEC submissions required column is missing.")
        columns[field] = column
    count = len(columns["form"])
    if any(len(column) != count for column in columns.values()):
        raise SecMetadataError("SEC submissions columns have unequal lengths.")
    rows: list[FilingMetadata] = []
    seen: set[str] = set()
    for index in range(count):
        form_type = columns["form"][index]
        if not isinstance(form_type, str) or form_type not in TARGET_FORMS:
            continue
        accession = normalize_accession_number(columns["accessionNumber"][index])
        if accession in seen:
            raise SecMetadataError("SEC submissions contains a duplicate target accession.")
        seen.add(accession)
        raw_accepted = columns["acceptanceDateTime"][index]
        if not isinstance(raw_accepted, str):
            raise SecMetadataError("SEC acceptance time is missing.")
        try:
            local_time = datetime.fromisoformat(raw_accepted)
        except ValueError:
            raise SecMetadataError("SEC acceptance time is malformed.") from None
        if local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=_EASTERN)
        accepted_at = local_time.astimezone(UTC)
        raw_period = columns["reportDate"][index]
        if raw_period in (None, ""):
            period = None
        elif isinstance(raw_period, str):
            try:
                period = date.fromisoformat(raw_period)
            except ValueError:
                raise SecMetadataError("SEC report period is malformed.") from None
        else:
            raise SecMetadataError("SEC report period is malformed.")
        primary = _filename(columns["primaryDocument"][index])
        archive_root = filing_archive_root(cik=cik, accession_number=accession)
        rows.append(
            FilingMetadata(
                accession_number=accession,
                form_type=form_type,
                accepted_at=accepted_at,
                period_of_report=period,
                primary_document=primary,
                filing_url=archive_root + quote(primary),
                raw_position=index + 1,
            )
        )
    return tuple(rows)


def filing_archive_root(*, cik: str, accession_number: str) -> str:
    accession = normalize_accession_number(accession_number)
    if not isinstance(cik, str) or not re.fullmatch(r"[0-9]{10}", cik):
        raise SecMetadataError("SEC CIK must be ten digits.")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def parse_filing_index(
    payload: bytes, *, cik: str, accession_number: str, primary_document: str
) -> tuple[DocumentMetadata, ...]:
    data = _object(payload)
    directory = data.get("directory")
    if not isinstance(directory, dict) or not isinstance(directory.get("item"), list):
        raise SecMetadataError("SEC filing directory is missing items.")
    root = filing_archive_root(cik=cik, accession_number=accession_number)
    expected_path = root.removeprefix("https://www.sec.gov").rstrip("/")
    directory_name = directory.get("name")
    if directory_name not in (expected_path, expected_path + "/"):
        raise SecMetadataError("SEC filing directory identity does not match the accession.")
    documents: list[DocumentMetadata] = []
    names: set[str] = set()
    for item in directory["item"]:
        if not isinstance(item, dict):
            raise SecMetadataError("SEC filing directory item is malformed.")
        name = item.get("name")
        item_type = item.get("type")
        if item_type == "directory":
            continue
        filename = _filename(name)
        if filename in names:
            raise SecMetadataError("SEC filing directory contains duplicate filenames.")
        names.add(filename)
        if not isinstance(item_type, str) or not item_type.strip() or len(item_type) > 100:
            raise SecMetadataError("SEC filing document type is malformed.")
        documents.append(
            DocumentMetadata(
                filename=filename,
                document_type=item_type.strip(),
                url=root + quote(filename),
            )
        )
    if primary_document not in names:
        raise SecMetadataError("SEC primary document is absent from the filing directory.")
    return tuple(documents)
