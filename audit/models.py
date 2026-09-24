import re
import uuid
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.base import ModelBase
from django.db.models.functions import Length
from django.utils import timezone

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.security import validate_safe_base_url


class DomainTargetType(models.TextChoices):
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


class AuditRecordTargetType(models.TextChoices):
    USER = "user", "User"
    DATA_SOURCE = "data_source", "Data source"
    SYNC_RUN = "sync_run", "Sync run"
    RAW_DATA_RECORD = "raw_data_record", "Raw data record"
    RAW_DATA_OBSERVATION = "raw_data_observation", "Raw data observation"
    SOURCE_EVIDENCE = "source_evidence", "Source evidence"
    DATA_CHANGE = "data_change", "Data change"
    COMPANY = "company", "Company"
    SECURITY_LISTING = "security_listing", "Security listing"
    MARKET_INDEX = "market_index", "Market index"
    INDEX_MEMBERSHIP = "index_membership", "Index membership"
    INDEX_CHANGE_EVENT = "index_change_event", "Index change event"
    INDEX_CHANGE_LEG = "index_change_leg", "Index change leg"
    EARNINGS_EVENT = "earnings_event", "Earnings event"
    EARNINGS_DATE_CHANGE = "earnings_date_change", "Earnings date change"
    EARNINGS_RECONCILIATION_DECISION = (
        "earnings_reconciliation_decision",
        "Earnings reconciliation decision",
    )
    FILING = "filing", "Filing"
    FILING_DOCUMENT = "filing_document", "Filing document"
    FILING_EARNINGS_LINK = "filing_earnings_link", "Filing earnings link"


DOMAIN_TARGET_TYPE_VALUES = tuple(value for value, _ in DomainTargetType.choices)
AUDIT_RECORD_TARGET_TYPE_VALUES = tuple(value for value, _ in AuditRecordTargetType.choices)


class AppendOnlyRecordError(RuntimeError):
    pass


class AppendOnlyQuerySet(models.QuerySet[Any]):
    def update(self, **kwargs: Any) -> int:
        del kwargs
        raise AppendOnlyRecordError("Audit history records cannot be updated.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise AppendOnlyRecordError("Audit history records cannot be deleted.")

    def bulk_update(
        self,
        objs: Any,
        fields: Any,
        batch_size: Any | None = None,
    ) -> int:
        del objs, fields, batch_size
        raise AppendOnlyRecordError("Audit history records cannot be bulk updated.")


class AppendOnlyAuditModel(models.Model):
    class Meta:
        abstract = True

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if not self._state.adding:
            raise AppendOnlyRecordError("Audit history records cannot be updated.")
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self,
        using: Any | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        del using, keep_parents
        raise AppendOnlyRecordError("Audit history records cannot be deleted.")


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

    class RunMode(models.TextChoices):
        INGESTION = "ingestion", "Ingestion"
        REPLAY = "replay", "Offline replay"

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
    run_mode = models.CharField(
        max_length=16,
        choices=RunMode.choices,
        default=RunMode.INGESTION,
    )
    replay_source_sync_run = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="replay_runs",
        null=True,
        blank=True,
    )
    replay_contract_version = models.CharField(max_length=100, blank=True, default="")
    replay_input_digest = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    fetched_count = models.PositiveBigIntegerField(default=0)
    replayed_count = models.PositiveBigIntegerField(default=0)
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
            models.UniqueConstraint(
                fields=(
                    "source",
                    "job_type",
                    "replay_source_sync_run",
                    "parser_version",
                    "replay_contract_version",
                    "replay_input_digest",
                ),
                condition=Q(run_mode="replay"),
                name="audit_sync_run_replay_identity_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(fetched_count__gte=0)
                    & Q(replayed_count__gte=0)
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
            models.CheckConstraint(
                condition=Q(run_mode__in=("ingestion", "replay")),
                name="audit_sync_run_mode_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(run_mode="ingestion")
                        & (Q(scope__window_kind__isnull=True) | ~Q(scope__window_kind="replay"))
                        & Q(replay_source_sync_run__isnull=True)
                        & Q(replay_contract_version="")
                        & Q(replay_input_digest="")
                        & Q(replayed_count=0)
                    )
                    | (
                        Q(run_mode="replay")
                        & Q(scope__window_kind="replay")
                        & Q(replay_source_sync_run__isnull=False)
                        & Q(replay_contract_version__regex=r"[^[:space:]]")
                        & Q(replay_input_digest__regex=r"^[0-9a-f]{64}$")
                        & Q(parser_version__regex=r"[^[:space:]]")
                        & Q(fetched_count=0)
                    )
                ),
                name="audit_sync_run_replay_metadata_consistent",
            ),
            models.CheckConstraint(
                condition=Q(replay_source_sync_run__isnull=True)
                | ~Q(pk=F("replay_source_sync_run")),
                name="audit_sync_run_replay_source_not_self",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.job_type}: {self.status} ({self.id})"

    def clean(self) -> None:
        super().clean()
        is_replay_scope = isinstance(self.scope, dict) and self.scope.get("window_kind") == "replay"
        if self.run_mode == self.RunMode.INGESTION:
            if is_replay_scope:
                raise ValidationError({"scope": "Ingestion runs cannot use replay window_kind."})
            if self.replay_source_sync_run_id is not None:
                raise ValidationError(
                    {"replay_source_sync_run": "Ingestion runs cannot have a replay source."}
                )
            if self.replay_contract_version or self.replay_input_digest or self.replayed_count:
                raise ValidationError("Ingestion runs cannot contain replay metadata or progress.")
            return
        if self.run_mode != self.RunMode.REPLAY:
            raise ValidationError({"run_mode": "Unknown SyncRun mode."})
        if not is_replay_scope:
            raise ValidationError({"scope": 'Replay runs require window_kind="replay".'})
        if self.replay_source_sync_run_id is None:
            raise ValidationError(
                {"replay_source_sync_run": "Replay runs require a source SyncRun."}
            )
        if self.pk is not None and self.replay_source_sync_run_id == self.pk:
            raise ValidationError(
                {"replay_source_sync_run": "A replay run cannot reference itself."}
            )
        if not self.replay_contract_version.strip():
            raise ValidationError(
                {"replay_contract_version": "Replay runs require a contract version."}
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.replay_input_digest):
            raise ValidationError(
                {"replay_input_digest": "Replay input digest must be a SHA-256 hex digest."}
            )
        if not self.parser_version.strip():
            raise ValidationError({"parser_version": "Replay runs require a parser version."})
        if self.fetched_count:
            raise ValidationError({"fetched_count": "Replay runs must not fetch provider data."})
        if self.replay_source_sync_run is not None:
            if self.source_id != self.replay_source_sync_run.source_id:
                raise ValidationError("Replay and source SyncRun must use the same DataSource.")
            if self.job_type != self.replay_source_sync_run.job_type:
                raise ValidationError("Replay and source SyncRun must use the same job type.")


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


class RawDataParseAttempt(AppendOnlyAuditModel):
    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        DATA_ERROR = "data_error", "Data error"
        SYSTEM_ERROR = "system_error", "System error"
        UNSUPPORTED = "unsupported", "Unsupported"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(
        RawDataObservation,
        on_delete=models.PROTECT,
        related_name="parse_attempts",
    )
    parser_version = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_summary = models.CharField(max_length=2000, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-finished_at",)
        indexes = [
            models.Index(fields=("observation", "parser_version")),
            models.Index(fields=("status", "finished_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("observation", "parser_version"),
                name="audit_raw_parse_observation_parser_unique",
            ),
            models.CheckConstraint(
                condition=Q(parser_version__regex=r"[^[:space:]]"),
                name="audit_raw_parse_parser_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(finished_at__gte=F("started_at")),
                name="audit_raw_parse_finish_after_start",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("succeeded", "data_error", "system_error", "unsupported")),
                name="audit_raw_parse_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status="succeeded") & Q(error_summary=""))
                    | (
                        Q(status__in=("data_error", "system_error", "unsupported"))
                        & Q(error_summary__regex=r"[^[:space:]]")
                    )
                ),
                name="audit_raw_parse_error_consistent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.observation_id}: {self.parser_version}: {self.status}"


class SourceEvidence(models.Model):
    TargetType = DomainTargetType

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
                condition=Q(target_type__in=DOMAIN_TARGET_TYPE_VALUES),
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


class DataChange(AppendOnlyAuditModel):
    TargetType = DomainTargetType

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_type = models.CharField(max_length=32, choices=TargetType.choices)
    target_id = models.UUIDField()
    field_name = models.CharField(max_length=100)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    source_evidence = models.ForeignKey(
        SourceEvidence,
        on_delete=models.PROTECT,
        related_name="data_changes",
        null=True,
        blank=True,
    )
    sync_run = models.ForeignKey(
        SyncRun,
        on_delete=models.PROTECT,
        related_name="data_changes",
        null=True,
        blank=True,
    )
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="data_changes",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=2000, blank=True)
    origin_key = models.CharField(max_length=255)
    rule_version = models.CharField(max_length=100)
    change_key = models.CharField(max_length=64, unique=True, editable=False)
    changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-changed_at",)
        indexes = [
            models.Index(fields=("target_type", "target_id", "field_name", "changed_at")),
            models.Index(fields=("sync_run", "changed_at")),
            models.Index(fields=("actor_user", "changed_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(target_type__in=DOMAIN_TARGET_TYPE_VALUES),
                name="audit_data_change_target_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(field_name__regex=r"[^[:space:]]"),
                name="audit_data_change_field_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(rule_version__regex=r"[^[:space:]]"),
                name="audit_data_change_rule_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(origin_key__regex=r"[^[:space:]]"),
                name="audit_data_change_origin_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(change_key__regex=r"^[0-9a-f]{64}$"),
                name="audit_data_change_key_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(old_value__isnull=True) & Q(new_value__isnull=False))
                    | (Q(old_value__isnull=False) & Q(new_value__isnull=True))
                    | (
                        Q(old_value__isnull=False)
                        & Q(new_value__isnull=False)
                        & ~Q(old_value=F("new_value"))
                    )
                ),
                name="audit_data_change_values_differ",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(actor_user__isnull=False) & Q(reason__regex=r"[^[:space:]]"))
                    | (
                        Q(actor_user__isnull=True)
                        & (Q(sync_run__isnull=False) | Q(source_evidence__isnull=False))
                    )
                ),
                name="audit_data_change_source_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.target_type}:{self.target_id}:{self.field_name}"


class AuditRecord(AppendOnlyAuditModel):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DEACTIVATE = "deactivate", "Deactivate"
        MANUAL_CORRECTION = "manual_correction", "Manual correction"
        MANUAL_SYNC = "manual_sync", "Manual sync"
        RETRY = "retry", "Retry"
        LOGIN_SENSITIVE_ACTION = "login_sensitive_action", "Login-sensitive action"

    TargetType = AuditRecordTargetType

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_records",
        null=True,
        blank=True,
    )
    sync_run = models.ForeignKey(
        SyncRun,
        on_delete=models.PROTECT,
        related_name="audit_records",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    target_type = models.CharField(max_length=32, choices=TargetType.choices)
    target_id = models.UUIDField()
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=2000, blank=True)
    request_id = models.CharField(max_length=255)
    ip_hash = models.CharField(max_length=67, blank=True)
    audit_key = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("target_type", "target_id", "action", "created_at")),
            models.Index(fields=("actor_user", "created_at")),
            models.Index(fields=("sync_run", "created_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    action__in=(
                        "create",
                        "update",
                        "deactivate",
                        "manual_correction",
                        "manual_sync",
                        "retry",
                        "login_sensitive_action",
                    )
                ),
                name="audit_record_action_valid",
            ),
            models.CheckConstraint(
                condition=Q(target_type__in=AUDIT_RECORD_TARGET_TYPE_VALUES),
                name="audit_record_target_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(actor_user__isnull=False) & Q(reason__regex=r"[^[:space:]]"))
                    | (Q(actor_user__isnull=True) & Q(sync_run__isnull=False))
                ),
                name="audit_record_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(request_id__regex=r"[^[:space:]]"),
                name="audit_record_request_id_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(ip_hash="") | Q(ip_hash__regex=r"^v1:[0-9a-f]{64}$"),
                name="audit_record_ip_hash_valid",
            ),
            models.CheckConstraint(
                condition=Q(audit_key__regex=r"^[0-9a-f]{64}$"),
                name="audit_record_key_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action}:{self.target_type}:{self.target_id}"
