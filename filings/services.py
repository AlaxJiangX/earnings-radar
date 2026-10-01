"""Controlled, provenance-checked Filing and document writes."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from audit.models import DomainTargetType, RawDataRecord, SyncRun
from audit.services import (
    record_source_evidence,
    record_system_action,
    resolve_source_evidence_reference,
)
from companies.models import Company
from companies.services import normalize_cik
from filings.models import Filing, FilingDocument
from filings.parsing import PARSER_VERSION, DocumentMetadata, FilingMetadata


class FilingIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FilingWriteResult:
    filing: Filing
    filing_created: bool
    documents_created: int


def filing_is_complete(*, metadata: FilingMetadata, company_id: object) -> bool:
    """Check an existing accession without silently accepting changed SEC metadata."""

    filing = Filing.objects.filter(accession_number=metadata.accession_number).first()
    if filing is None:
        return False
    _verify_filing(filing=filing, metadata=metadata, company_id=company_id)
    filing_evidence = filing.source_evidence
    if filing_evidence is None:
        raise FilingIntegrityError("Existing Filing is missing its source evidence.")
    resolve_source_evidence_reference(
        source_evidence=filing_evidence,
        sync_run=None,
        target_type=DomainTargetType.FILING,
        target_id=filing.pk,
    )
    primary = (
        FilingDocument.objects.filter(filing=filing, filename=filing.primary_document)
        .select_related("source_evidence")
        .first()
    )
    if primary is None:
        return False
    primary_evidence = primary.source_evidence
    if primary.url != filing.filing_url or primary_evidence is None:
        raise FilingIntegrityError(
            "Existing primary FilingDocument metadata or evidence is invalid."
        )
    resolve_source_evidence_reference(
        source_evidence=primary_evidence,
        sync_run=None,
        target_type=DomainTargetType.FILING_DOCUMENT,
        target_id=primary.pk,
    )
    return True


def record_filing(
    *,
    company: Company,
    requested_cik: str,
    metadata: FilingMetadata,
    documents: tuple[DocumentMetadata, ...],
    submissions_raw: RawDataRecord,
    directory_raw: RawDataRecord,
    sync_run: SyncRun,
) -> FilingWriteResult:
    """Write one accession atomically after both SEC metadata responses were observed."""

    if not isinstance(requested_cik, str) or normalize_cik(requested_cik) != requested_cik:
        raise FilingIntegrityError("Requested SEC CIK is not canonical.")
    with transaction.atomic():
        current_company = Company.objects.select_for_update().get(pk=company.pk)
        if current_company.cik != requested_cik:
            raise FilingIntegrityError("Company CIK changed after the SEC request.")
        if not documents or metadata.primary_document not in {item.filename for item in documents}:
            raise FilingIntegrityError("Filing has no verified primary document.")
        defaults = {
            "company": current_company,
            "form_type": metadata.form_type,
            "accepted_at": metadata.accepted_at,
            "period_of_report": metadata.period_of_report,
            "primary_document": metadata.primary_document,
            "filing_url": metadata.filing_url,
        }
        try:
            with transaction.atomic():
                filing = Filing.objects.create(
                    accession_number=metadata.accession_number, **defaults
                )
                filing_created = True
        except IntegrityError:
            filing = Filing.objects.select_for_update().get(
                accession_number=metadata.accession_number
            )
            filing_created = False
        _verify_filing(filing=filing, metadata=metadata, company_id=current_company.pk)
        filing_evidence = record_source_evidence(
            raw_data_record=submissions_raw,
            sync_run=sync_run,
            target_type=DomainTargetType.FILING,
            target_id=filing.pk,
            field_name="",
            raw_value={
                "accession_number": metadata.accession_number,
                "position": metadata.raw_position,
            },
            normalized_value=_filing_values(metadata),
            confidence="1",
            normalizer_version=PARSER_VERSION,
        ).evidence
        resolve_source_evidence_reference(
            source_evidence=filing_evidence,
            sync_run=None,
            target_type=DomainTargetType.FILING,
            target_id=filing.pk,
        )
        if filing_created:
            filing.source_evidence = filing_evidence
            filing.save(update_fields=("source_evidence", "updated_at"))
            record_system_action(
                sync_run=sync_run,
                action="create",
                target_type=DomainTargetType.FILING,
                target_id=filing.pk,
                before=None,
                after=_filing_values(metadata),
                request_id=f"sec-filing:{metadata.accession_number}",
            )
        elif filing.source_evidence_id is None:
            raise FilingIntegrityError("Existing Filing is missing its source evidence.")

        created_count = 0
        for document in documents:
            try:
                with transaction.atomic():
                    row = FilingDocument.objects.create(
                        filing=filing,
                        filename=document.filename,
                        document_type=document.document_type,
                        sequence=None,
                        url=document.url,
                        description=document.description,
                    )
                    created = True
            except IntegrityError:
                row = FilingDocument.objects.select_for_update().get(
                    filing=filing, filename=document.filename
                )
                created = False
            if (
                row.url != document.url
                or row.document_type != document.document_type
                or row.description != document.description
                or row.sequence is not None
            ):
                raise FilingIntegrityError("Existing FilingDocument metadata conflicts with SEC.")
            evidence = record_source_evidence(
                raw_data_record=directory_raw,
                sync_run=sync_run,
                target_type=DomainTargetType.FILING_DOCUMENT,
                target_id=row.pk,
                field_name="",
                raw_value={"filename": document.filename},
                normalized_value={
                    "filename": document.filename,
                    "document_type": document.document_type,
                    "url": document.url,
                },
                confidence="1",
                normalizer_version=PARSER_VERSION,
            ).evidence
            resolve_source_evidence_reference(
                source_evidence=evidence,
                sync_run=None,
                target_type=DomainTargetType.FILING_DOCUMENT,
                target_id=row.pk,
            )
            if created:
                row.source_evidence = evidence
                row.save(update_fields=("source_evidence",))
                record_system_action(
                    sync_run=sync_run,
                    action="create",
                    target_type=DomainTargetType.FILING_DOCUMENT,
                    target_id=row.pk,
                    before=None,
                    after={"filing": str(filing.pk), "filename": document.filename},
                    request_id=f"sec-document:{metadata.accession_number}:{document.filename}",
                )
                created_count += 1
            elif row.source_evidence_id is None:
                raise FilingIntegrityError(
                    "Existing FilingDocument is missing its source evidence."
                )

        return FilingWriteResult(
            filing=filing,
            filing_created=filing_created,
            documents_created=created_count,
        )


def _filing_values(metadata: FilingMetadata) -> dict[str, object]:
    return {
        "accession_number": metadata.accession_number,
        "form_type": metadata.form_type,
        "accepted_at": metadata.accepted_at.isoformat(),
        "period_of_report": metadata.period_of_report.isoformat()
        if metadata.period_of_report
        else None,
        "primary_document": metadata.primary_document,
        "filing_url": metadata.filing_url,
    }


def _verify_filing(*, filing: Filing, metadata: FilingMetadata, company_id: object) -> None:
    if (
        filing.company_id != company_id
        or filing.form_type != metadata.form_type
        or filing.accepted_at != metadata.accepted_at
        or filing.period_of_report != metadata.period_of_report
        or filing.primary_document != metadata.primary_document
        or filing.filing_url != metadata.filing_url
    ):
        raise FilingIntegrityError("Existing Filing identity or metadata conflicts with SEC.")
