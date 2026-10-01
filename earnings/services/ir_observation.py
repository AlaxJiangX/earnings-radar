"""Append-only persistence primitive for InvestorRelationsObservation rows."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from audit.models import DataSource, RawDataRecord
from audit.security import AuditSecurityError, normalize_json_without_credentials
from companies.models import Company
from earnings.models import (
    ALLOWED_INVESTOR_RELATIONS_CANCELLATION_SCOPES,
    ALLOWED_INVESTOR_RELATIONS_ITEM_TYPES,
    ALLOWED_PERIOD_TYPES,
    ALLOWED_RELEASE_SESSIONS,
    INTERNAL_IR_SOURCE_IDENTITY_PATTERN,
    EarningsDatePrecision,
    InvestorRelationsObservation,
)

_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_INTERNAL_IDENTITY_RE = re.compile(INTERNAL_IR_SOURCE_IDENTITY_PATTERN)
_CONFIDENCE_QUANTUM = Decimal("0.0001")
_UNIQUE_VIOLATION_SQLSTATE = "23505"
_OBSERVATION_UNIQUE_CONSTRAINT = "investor_relations_observation_record_parser_event_unique"


class InvestorRelationsObservationServiceError(ValueError):
    """Base error for the IR observation persistence primitive."""


class InvalidInvestorRelationsObservation(InvestorRelationsObservationServiceError):
    pass


class InvestorRelationsObservationIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InvestorRelationsObservationWriteResult:
    observation: InvestorRelationsObservation
    created: bool


@dataclass(frozen=True, slots=True)
class _NormalizedIRDateFact:
    date_value: date | None
    datetime_value: datetime | None
    precision: str


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    provider_key: str
    provider_version: str
    parser_version: str
    source_event_identity: str
    raw_position: int
    period_end_date: date
    period_type: str
    item_type: str
    estimated_release: _NormalizedIRDateFact
    confirmed_release: _NormalizedIRDateFact
    earnings_release: _NormalizedIRDateFact
    conference_call: _NormalizedIRDateFact
    release_session: str | None
    cancellation: dict[str, object] | None
    source_observed_at: datetime | None
    confidence: Decimal | None

    def as_create_kwargs(self) -> dict[str, object]:
        return {
            "provider_key": self.provider_key,
            "provider_version": self.provider_version,
            "parser_version": self.parser_version,
            "source_event_identity": self.source_event_identity,
            "raw_position": self.raw_position,
            "period_end_date": self.period_end_date,
            "period_type": self.period_type,
            "item_type": self.item_type,
            "estimated_release_at": self.estimated_release.datetime_value,
            "estimated_release_date": self.estimated_release.date_value,
            "estimated_release_precision": self.estimated_release.precision,
            "confirmed_release_at": self.confirmed_release.datetime_value,
            "confirmed_release_date": self.confirmed_release.date_value,
            "confirmed_release_precision": self.confirmed_release.precision,
            "earnings_release_at": self.earnings_release.datetime_value,
            "earnings_release_date": self.earnings_release.date_value,
            "earnings_release_precision": self.earnings_release.precision,
            "conference_call_at": self.conference_call.datetime_value,
            "conference_call_date": self.conference_call.date_value,
            "conference_call_precision": self.conference_call.precision,
            "release_session": self.release_session,
            "cancellation": self.cancellation,
            "source_observed_at": self.source_observed_at,
            "confidence": self.confidence,
        }


def record_investor_relations_observation(
    *,
    source: DataSource,
    raw_data_record: RawDataRecord,
    company: Company,
    provider_key: str,
    provider_version: str,
    parser_version: str,
    source_event_identity: str,
    raw_position: int,
    period_end_date: date,
    period_type: str,
    item_type: str,
    estimated_release: date | datetime | None = None,
    confirmed_release: date | datetime | None = None,
    earnings_release: date | datetime | None = None,
    conference_call: date | datetime | None = None,
    release_session: str | None = None,
    cancellation: Mapping[str, object] | None = None,
    source_observed_at: datetime | None = None,
    confidence: Decimal | int | float | str | None = None,
) -> InvestorRelationsObservationWriteResult:
    """Persist one normalized IR source fact without making decisions.

    This primitive validates persistence invariants only.  It does not parse
    payloads, resolve canonical EarningsEvents, decide authority, or mutate
    EarningsEvent schedule/status/lifecycle.
    """

    normalized = _normalize_observation_values(
        provider_key=provider_key,
        provider_version=provider_version,
        parser_version=parser_version,
        source_event_identity=source_event_identity,
        raw_position=raw_position,
        period_end_date=period_end_date,
        period_type=period_type,
        item_type=item_type,
        estimated_release=estimated_release,
        confirmed_release=confirmed_release,
        earnings_release=earnings_release,
        conference_call=conference_call,
        release_session=release_session,
        cancellation=cancellation,
        source_observed_at=source_observed_at,
        confidence=confidence,
    )

    with transaction.atomic():
        current_source = _load_persisted_source(source)
        current_company = _load_persisted_company(company)
        current_raw_record = _load_persisted_raw_data_record(raw_data_record)
        if current_raw_record.source_id != current_source.pk:
            raise InvalidInvestorRelationsObservation(
                "raw_data_record must belong to the supplied source."
            )
        if current_source.source_type != DataSource.SourceType.INVESTOR_RELATIONS:
            raise InvalidInvestorRelationsObservation(
                "source must use the investor_relations source type."
            )
        if current_source.provider_adapter.strip() != normalized.provider_key:
            raise InvalidInvestorRelationsObservation(
                "provider_key must match the source provider_adapter."
            )

        try:
            with transaction.atomic():
                observation = InvestorRelationsObservation.objects.create(
                    source=current_source,
                    raw_data_record=current_raw_record,
                    company=current_company,
                    **normalized.as_create_kwargs(),
                )
                return InvestorRelationsObservationWriteResult(
                    observation=observation,
                    created=True,
                )
        except IntegrityError as error:
            if not _is_observation_unique_violation(error):
                raise
            existing = InvestorRelationsObservation.objects.filter(
                raw_data_record=current_raw_record,
                parser_version=normalized.parser_version,
                source_event_identity=normalized.source_event_identity,
            ).first()
            if existing is None:
                raise
            _verify_existing_observation(
                observation=existing,
                source_id=current_source.pk,
                company_id=current_company.pk,
                raw_data_record_id=current_raw_record.pk,
                normalized=normalized,
            )
            return InvestorRelationsObservationWriteResult(
                observation=existing,
                created=False,
            )


def _normalize_observation_values(
    *,
    provider_key: str,
    provider_version: str,
    parser_version: str,
    source_event_identity: str,
    raw_position: int,
    period_end_date: date,
    period_type: str,
    item_type: str,
    estimated_release: date | datetime | None,
    confirmed_release: date | datetime | None,
    earnings_release: date | datetime | None,
    conference_call: date | datetime | None,
    release_session: str | None,
    cancellation: Mapping[str, object] | None,
    source_observed_at: datetime | None,
    confidence: Decimal | int | float | str | None,
) -> _NormalizedObservation:
    normalized_provider_key = _require_text(provider_key, "provider_key", 64)
    if not _PROVIDER_KEY_RE.fullmatch(normalized_provider_key):
        raise InvalidInvestorRelationsObservation(
            "provider_key must be a stable lowercase identifier."
        )
    normalized_identity = _require_text(source_event_identity, "source_event_identity", 255)
    if normalized_identity.startswith("internal:") and not _INTERNAL_IDENTITY_RE.fullmatch(
        normalized_identity
    ):
        raise InvalidInvestorRelationsObservation(
            "internal source_event_identity must use the internal:ir:v1: namespace."
        )
    normalized = _NormalizedObservation(
        provider_key=normalized_provider_key,
        provider_version=_require_text(provider_version, "provider_version", 100),
        parser_version=_require_text(parser_version, "parser_version", 100),
        source_event_identity=normalized_identity,
        raw_position=_require_int(raw_position, "raw_position", minimum=1),
        period_end_date=_require_date(period_end_date, "period_end_date"),
        period_type=_normalize_period_type(period_type),
        item_type=_normalize_item_type(item_type),
        estimated_release=_normalize_fact(estimated_release, "estimated_release"),
        confirmed_release=_normalize_fact(confirmed_release, "confirmed_release"),
        earnings_release=_normalize_fact(earnings_release, "earnings_release"),
        conference_call=_normalize_fact(conference_call, "conference_call"),
        release_session=_normalize_release_session(release_session),
        cancellation=_normalize_cancellation(cancellation),
        source_observed_at=_normalize_aware_datetime(
            source_observed_at,
            "source_observed_at",
        ),
        confidence=_normalize_confidence(confidence),
    )
    _validate_item_shape(normalized)
    return normalized


def _validate_item_shape(normalized: _NormalizedObservation) -> None:
    estimated = normalized.estimated_release.precision != EarningsDatePrecision.UNKNOWN
    confirmed = normalized.confirmed_release.precision != EarningsDatePrecision.UNKNOWN
    earnings = normalized.earnings_release.precision != EarningsDatePrecision.UNKNOWN
    conference = normalized.conference_call.precision != EarningsDatePrecision.UNKNOWN
    if normalized.item_type == "release_confirmation":
        if estimated == confirmed:
            raise InvalidInvestorRelationsObservation(
                "release_confirmation requires exactly one explicit confirmed_release "
                "or estimated_release fact."
            )
        if earnings:
            raise InvalidInvestorRelationsObservation(
                "release_confirmation must not declare an earnings release fact."
            )
    elif normalized.item_type == "results_release":
        if not earnings or estimated or confirmed:
            raise InvalidInvestorRelationsObservation(
                "results_release requires exactly one explicit earnings_release fact "
                "and no schedule facts."
            )
    elif normalized.item_type == "call_notice":
        if not conference or earnings or (estimated and confirmed):
            raise InvalidInvestorRelationsObservation(
                "call_notice requires an explicit conference_call fact, no earnings "
                "release fact, and at most one release schedule fact."
            )
    elif normalized.item_type == "cancellation":
        if normalized.cancellation is None:
            raise InvalidInvestorRelationsObservation(
                "cancellation requires an explicit cancellation structure."
            )
        if estimated or confirmed or earnings or conference:
            raise InvalidInvestorRelationsObservation(
                "cancellation items must not declare schedule or release facts."
            )


def _load_persisted_source(source: DataSource) -> DataSource:
    if source._state.adding or source.pk is None:
        raise InvalidInvestorRelationsObservation("source must be saved before use.")
    try:
        return DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvalidInvestorRelationsObservation("source no longer exists.") from error


def _load_persisted_company(company: Company) -> Company:
    if company._state.adding or company.pk is None:
        raise InvalidInvestorRelationsObservation("company must be saved before use.")
    try:
        return Company.objects.get(pk=company.pk)
    except Company.DoesNotExist as error:
        raise InvalidInvestorRelationsObservation("company no longer exists.") from error


def _load_persisted_raw_data_record(raw_data_record: RawDataRecord) -> RawDataRecord:
    if raw_data_record._state.adding or raw_data_record.pk is None:
        raise InvalidInvestorRelationsObservation("raw_data_record must be saved before use.")
    try:
        return RawDataRecord.objects.select_related("source").get(pk=raw_data_record.pk)
    except RawDataRecord.DoesNotExist as error:
        raise InvalidInvestorRelationsObservation("raw_data_record no longer exists.") from error


def _verify_existing_observation(
    *,
    observation: InvestorRelationsObservation,
    source_id: uuid.UUID,
    company_id: uuid.UUID,
    raw_data_record_id: uuid.UUID,
    normalized: _NormalizedObservation,
) -> None:
    if (
        observation.source_id != source_id
        or observation.company_id != company_id
        or observation.raw_data_record_id != raw_data_record_id
    ):
        raise InvestorRelationsObservationIntegrityError(
            "An existing InvestorRelationsObservation has the same key but different lineage."
        )
    for field_name, expected in normalized.as_create_kwargs().items():
        if getattr(observation, field_name) != expected:
            raise InvestorRelationsObservationIntegrityError(
                "An existing InvestorRelationsObservation has the same key "
                f"but different {field_name}."
            )


def _is_observation_unique_violation(error: IntegrityError) -> bool:
    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(cause, "pgcode", None)
    if sqlstate != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    constraint_name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    return constraint_name == _OBSERVATION_UNIQUE_CONSTRAINT


def _require_text(value: object, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsObservation(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidInvestorRelationsObservation(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidInvestorRelationsObservation(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _require_int(value: object, value_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInvestorRelationsObservation(f"{value_name} must be an integer.")
    if value < minimum:
        raise InvalidInvestorRelationsObservation(f"{value_name} must be at least {minimum}.")
    return value


def _require_date(value: object, value_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidInvestorRelationsObservation(f"{value_name} must be a date.")
    return value


def _normalize_period_type(value: object) -> str:
    normalized = _require_text(value, "period_type", 8).upper()
    if normalized not in ALLOWED_PERIOD_TYPES:
        raise InvalidInvestorRelationsObservation(
            "period_type must use the normalized earnings period enum."
        )
    return normalized


def _normalize_item_type(value: object) -> str:
    normalized = _require_text(value, "item_type", 32).lower()
    if normalized not in ALLOWED_INVESTOR_RELATIONS_ITEM_TYPES:
        raise InvalidInvestorRelationsObservation("item_type must use the supported IR item enum.")
    return normalized


def _normalize_fact(value: object, value_name: str) -> _NormalizedIRDateFact:
    if value is None:
        return _NormalizedIRDateFact(None, None, EarningsDatePrecision.UNKNOWN)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidInvestorRelationsObservation(
                f"{value_name} datetime must be timezone-aware."
            )
        return _NormalizedIRDateFact(
            None,
            value.astimezone(UTC),
            EarningsDatePrecision.EXACT_DATETIME,
        )
    if isinstance(value, date):
        return _NormalizedIRDateFact(value, None, EarningsDatePrecision.DATE_ONLY)
    raise InvalidInvestorRelationsObservation(
        f"{value_name} must be a date, timezone-aware datetime, or null."
    )


def _normalize_release_session(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInvestorRelationsObservation("release_session must be a string or null.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_RELEASE_SESSIONS:
        raise InvalidInvestorRelationsObservation(
            "release_session must use the supported session enum."
        )
    return normalized


def _normalize_cancellation(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidInvestorRelationsObservation("cancellation must be a JSON object or null.")
    if set(value) != {"scope", "reason_code"}:
        raise InvalidInvestorRelationsObservation(
            "cancellation must contain exactly 'scope' and 'reason_code'."
        )
    scope = value["scope"]
    reason_code = value["reason_code"]
    if not isinstance(scope, str) or scope.strip().lower() not in (
        ALLOWED_INVESTOR_RELATIONS_CANCELLATION_SCOPES
    ):
        raise InvalidInvestorRelationsObservation(
            "cancellation scope must be event or conference_call."
        )
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise InvalidInvestorRelationsObservation("cancellation reason_code must not be empty.")
    try:
        normalized = normalize_json_without_credentials(
            {
                "scope": scope.strip().lower(),
                "reason_code": reason_code.strip(),
            },
            value_name="cancellation",
        )
    except AuditSecurityError as error:
        raise InvalidInvestorRelationsObservation(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidInvestorRelationsObservation("cancellation must be a JSON object.")
    return normalized


def _normalize_aware_datetime(value: object, value_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInvestorRelationsObservation(
            f"{value_name} must be a timezone-aware datetime or null."
        )
    return value.astimezone(UTC)


def _normalize_confidence(
    value: Decimal | int | float | str | None,
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidInvestorRelationsObservation("confidence must be a number between 0 and 1.")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidInvestorRelationsObservation(
            "confidence must be a number between 0 and 1."
        ) from None
    if not normalized.is_finite() or not Decimal("0") <= normalized <= Decimal("1"):
        raise InvalidInvestorRelationsObservation("confidence must be a number between 0 and 1.")
    return normalized.quantize(_CONFIDENCE_QUANTUM)
