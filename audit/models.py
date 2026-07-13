import uuid

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Length
from django.utils import timezone

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.security import validate_safe_base_url


class DataSource(models.Model):
    class SourceType(models.TextChoices):
        SEC = "sec", "SEC"
        INVESTOR_RELATIONS = "ir", "Investor relations"
        EARNINGS_CALENDAR = "earnings_calendar", "Earnings calendar"
        INDEX = "index", "Index"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    base_url = models.URLField(blank=True, validators=(validate_safe_base_url,))
    is_official = models.BooleanField(default=False)
    provider_adapter = models.CharField(max_length=255, blank=True)
    license_notes = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key",)
        constraints = [
            models.CheckConstraint(
                condition=Q(source_type__in=("sec", "ir", "earnings_calendar", "index", "manual")),
                name="audit_data_source_type_valid",
            )
        ]

    def __str__(self) -> str:
        return self.name


class SyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_type = models.CharField(max_length=100)
    source = models.ForeignKey(
        DataSource,
        on_delete=models.PROTECT,
        related_name="sync_runs",
    )
    scope = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    fetched_count = models.PositiveBigIntegerField(default=0)
    created_count = models.PositiveBigIntegerField(default=0)
    updated_count = models.PositiveBigIntegerField(default=0)
    skipped_count = models.PositiveBigIntegerField(default=0)
    failed_count = models.PositiveBigIntegerField(default=0)
    error_summary = models.CharField(max_length=2000, blank=True)
    code_version = models.CharField(max_length=100, blank=True)
    parser_version = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [
            models.Index(fields=("job_type", "status", "started_at")),
            models.Index(fields=("source", "status", "started_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("source", "job_type", "idempotency_key"),
                name="audit_sync_run_window_key_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(fetched_count__gte=0)
                    & Q(created_count__gte=0)
                    & Q(updated_count__gte=0)
                    & Q(skipped_count__gte=0)
                    & Q(failed_count__gte=0)
                ),
                name="audit_sync_run_counts_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True) | Q(finished_at__gte=F("started_at")),
                name="audit_sync_run_finish_after_start",
            ),
            models.CheckConstraint(
                condition=Q(heartbeat_at__gte=F("started_at")),
                name="audit_sync_run_heartbeat_after_start",
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True) | Q(heartbeat_at__lte=F("finished_at")),
                name="audit_sync_run_heartbeat_before_finish",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status="running") & Q(finished_at__isnull=True))
                    | (
                        Q(status__in=("succeeded", "partial", "failed", "skipped"))
                        & Q(finished_at__isnull=False)
                    )
                ),
                name="audit_sync_run_status_finish_consistent",
            ),
            models.CheckConstraint(
                condition=~Q(job_type="") & ~Q(idempotency_key=""),
                name="audit_sync_run_keys_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.job_type}: {self.status} ({self.id})"


class RawDataRecord(models.Model):
    class ParserStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PARSED = "parsed", "Parsed"
        FAILED = "failed", "Failed"
        UNSUPPORTED = "unsupported", "Unsupported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        DataSource,
        on_delete=models.PROTECT,
        related_name="raw_data_records",
    )
    first_sync_run = models.ForeignKey(
        SyncRun,
        on_delete=models.PROTECT,
        related_name="first_raw_data_records",
    )
    source_url = models.URLField(max_length=2048)
    request_fingerprint = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    encoding = models.CharField(max_length=64, blank=True)
    content_hash = models.CharField(max_length=64)
    payload = models.BinaryField(max_length=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES)
    payload_size_bytes = models.PositiveIntegerField()
    parser_status = models.CharField(
        max_length=16,
        choices=ParserStatus.choices,
        default=ParserStatus.PENDING,
    )
    parser_version = models.CharField(max_length=100, blank=True)
    parse_error = models.CharField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-fetched_at",)
        indexes = [
            models.Index(fields=("source", "fetched_at")),
            models.Index(fields=("parser_status", "fetched_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("source", "request_fingerprint", "content_hash"),
                name="audit_raw_data_content_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(request_fingerprint__regex=r"^[0-9a-f]{64}$")
                    & Q(content_hash__regex=r"^[0-9a-f]{64}$")
                ),
                name="audit_raw_data_hashes_valid",
            ),
            models.CheckConstraint(
                condition=Q(payload_size_bytes=Length("payload")),
                name="audit_raw_data_payload_size_matches",
            ),
            models.CheckConstraint(
                condition=Q(payload_size_bytes__lte=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES),
                name="audit_raw_data_payload_size_limited",
            ),
            models.CheckConstraint(
                condition=(
                    Q(http_status__isnull=True)
                    | (Q(http_status__gte=100) & Q(http_status__lte=599))
                ),
                name="audit_raw_data_http_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(parser_status="pending") & Q(parser_version="") & Q(parse_error=""))
                    | (Q(parser_status="parsed") & ~Q(parser_version="") & Q(parse_error=""))
                    | (Q(parser_status="failed") & ~Q(parser_version="") & ~Q(parse_error=""))
                    | (Q(parser_status="unsupported") & ~Q(parser_version="") & Q(parse_error=""))
                ),
                name="audit_raw_data_parser_state_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source.key}: {self.content_hash}"


class RawDataObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sync_run = models.ForeignKey(
        SyncRun,
        on_delete=models.PROTECT,
        related_name="raw_data_observations",
    )
    raw_data_record = models.ForeignKey(
        RawDataRecord,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-observed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("sync_run", "raw_data_record"),
                name="audit_raw_observation_run_record_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sync_run_id}: {self.raw_data_record_id}"


class SourceEvidence(models.Model):
    class TargetType(models.TextChoices):
        COMPANY = "company", "Company"
        SECURITY_LISTING = "security_listing", "Security listing"
        MARKET_INDEX = "market_index", "Market index"
        INDEX_MEMBERSHIP = "index_membership", "Index membership"
        INDEX_CHANGE_EVENT = "index_change_event", "Index change event"
        INDEX_CHANGE_LEG = "index_change_leg", "Index change leg"
        EARNINGS_EVENT = "earnings_event", "Earnings event"
        EARNINGS_DATE_CHANGE = "earnings_date_change", "Earnings date change"
        FILING = "filing", "Filing"
        FILING_DOCUMENT = "filing_document", "Filing document"
        FILING_EARNINGS_LINK = "filing_earnings_link", "Filing earnings link"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_data_record = models.ForeignKey(
        RawDataRecord,
        on_delete=models.PROTECT,
        related_name="source_evidence_records",
    )
    sync_run = models.ForeignKey(
        SyncRun,
        on_delete=models.PROTECT,
        related_name="source_evidence_records",
    )
    target_type = models.CharField(max_length=32, choices=TargetType.choices)
    target_id = models.UUIDField()
    field_name = models.CharField(max_length=100, blank=True)
    raw_value = models.JSONField(null=True, blank=True)
    normalized_value = models.JSONField(null=True, blank=True)
    is_official = models.BooleanField()
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    observed_at = models.DateTimeField()
    normalizer_version = models.CharField(max_length=100)
    evidence_key = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-observed_at",)
        indexes = [
            models.Index(fields=("target_type", "target_id", "field_name", "observed_at")),
            models.Index(fields=("raw_data_record", "sync_run")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    target_type__in=(
                        "company",
                        "security_listing",
                        "market_index",
                        "index_membership",
                        "index_change_event",
                        "index_change_leg",
                        "earnings_event",
                        "earnings_date_change",
                        "filing",
                        "filing_document",
                        "filing_earnings_link",
                    )
                ),
                name="audit_source_evidence_target_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1),
                name="audit_source_evidence_confidence_range",
            ),
            models.CheckConstraint(
                condition=Q(normalizer_version__regex=r"[^[:space:]]"),
                name="audit_source_evidence_normalizer_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(evidence_key__regex=r"^[0-9a-f]{64}$"),
                name="audit_source_evidence_key_valid",
            ),
        ]

    def __str__(self) -> str:
        field = self.field_name or "<record>"
        return f"{self.target_type}:{self.target_id}:{field}"
