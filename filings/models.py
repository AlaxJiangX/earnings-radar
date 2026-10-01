from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from audit.security import validate_safe_base_url

TARGET_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F", "40-F")


class Filing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company", on_delete=models.PROTECT, related_name="filings"
    )
    accession_number = models.CharField(max_length=20, unique=True)
    form_type = models.CharField(max_length=5)
    accepted_at = models.DateTimeField()
    period_of_report = models.DateField(null=True, blank=True)
    primary_document = models.CharField(max_length=255)
    filing_url = models.URLField(max_length=500, validators=(validate_safe_base_url,))
    source_evidence = models.ForeignKey(
        "audit.SourceEvidence", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("company", "-accepted_at"))]
        constraints = [
            models.CheckConstraint(
                condition=Q(accession_number__regex=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$"),
                name="filings_accession_format",
            ),
            models.CheckConstraint(
                condition=Q(form_type__in=TARGET_FORMS), name="filings_form_target"
            ),
        ]

    def __str__(self) -> str:
        return self.accession_number


class FilingDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filing = models.ForeignKey(Filing, on_delete=models.PROTECT, related_name="documents")
    document_type = models.CharField(max_length=100)
    sequence = models.PositiveIntegerField(null=True, blank=True)
    filename = models.CharField(max_length=255)
    url = models.URLField(max_length=500, validators=(validate_safe_base_url,))
    description = models.CharField(max_length=500, blank=True)
    source_evidence = models.ForeignKey(
        "audit.SourceEvidence", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("filing", "filename"), name="filings_doc_filename_unique"
            ),
            models.CheckConstraint(condition=~Q(filename=""), name="filings_doc_filename_nonempty"),
            models.CheckConstraint(
                condition=~Q(document_type=""), name="filings_doc_type_nonempty"
            ),
        ]

    def __str__(self) -> str:
        return self.filename
