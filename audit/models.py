import uuid

from django.db import models
from django.db.models import F, Q
from django.utils import timezone


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
    base_url = models.URLField(blank=True)
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
