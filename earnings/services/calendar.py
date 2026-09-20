from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import DataSource, RawDataRecord
from companies.services import CompanyServiceError, normalize_cik
from earnings.models import (
    ALLOWED_EARNINGS_DATE_PRECISIONS,
    ALLOWED_FISCAL_CALENDAR_TYPES,
    ALLOWED_PERIOD_TYPES,
    ALLOWED_RELEASE_SESSIONS,
    EarningsCalendarObservation,
    EarningsDatePrecision,
    ReleaseSession,
)

_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_CONFIDENCE_QUANTUM = Decimal("0.0001")
_UNIQUE_VIOLATION_SQLSTATE = "23505"
_OBSERVATION_UNIQUE_CONSTRAINT = "earnings_calendar_observation_record_parser_event_unique"


class EarningsCalendarObservationServiceError(ValueError):
    """Base error for the earnings calendar observation persistence primitive."""


class InvalidEarningsCalendarObservation(EarningsCalendarObservationServiceError):
    pass


class EarningsCalendarObservationIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EarningsCalendarObservationWriteResult:
    observation: EarningsCalendarObservation
    created: bool


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    provider_key: str
    provider_version: str
    parser_version: str
    provider_event_id: str
    raw_position: int
    cik: str
    ticker: str
    exchange: str
    provider_symbol: str
    company_name: str
    fiscal_label_raw: str
    fiscal_year: int | None
    period_end_date: date | None
    period_type: str | None
    fiscal_calendar_type: str | None
    period_length_weeks: int | None
    estimated_release_date: date | None
    estimated_release_at: datetime | None
    estimated_release_precision: str
    release_session: str
    source_observed_at: datetime | None
    confidence: Decimal | None

    def as_create_kwargs(self) -> dict[str, object]:
        return {
            "provider_key": self.provider_key,
            "provider_version": self.provider_version,
            "parser_version": self.parser_version,
            "provider_event_id": self.provider_event_id,
            "raw_position": self.raw_position,
            "cik": self.cik,
            "ticker": self.ticker,
            "exchange": self.exchange,
            "provider_symbol": self.provider_symbol,
            "company_name": self.company_name,
            "fiscal_label_raw": self.fiscal_label_raw,
            "fiscal_year": self.fiscal_year,
            "period_end_date": self.period_end_date,
            "period_type": self.period_type,
            "fiscal_calendar_type": self.fiscal_calendar_type,
            "period_length_weeks": self.period_length_weeks,
            "estimated_release_date": self.estimated_release_date,
            "estimated_release_at": self.estimated_release_at,
            "estimated_release_precision": self.estimated_release_precision,
            "release_session": self.release_session,
            "source_observed_at": self.source_observed_at,
            "confidence": self.confidence,
        }


def record_earnings_calendar_observation(
    *,
    source: DataSource,
    raw_data_record: RawDataRecord,
    provider_key: str,
    provider_version: str,
    parser_version: str,
    provider_event_id: str,
    raw_position: int,
    cik: str | int | None = "",
    ticker: str = "",
    exchange: str = "",
    provider_symbol: str = "",
    company_name: str = "",
    fiscal_label_raw: str = "",
    fiscal_year: int | None = None,
    period_end_date: date | None = None,
    period_type: str | None = None,
    fiscal_calendar_type: str | None = None,
    period_length_weeks: int | None = None,
    estimated_release: date | datetime | None = None,
    estimated_release_precision: str | None = None,
    release_session: str = ReleaseSession.UNKNOWN,
    source_observed_at: datetime | None = None,
    confidence: Decimal | int | float | str | None = None,
) -> EarningsCalendarObservationWriteResult:
    """Persist one provider-neutral earnings calendar observation.

    This primitive validates persistence invariants only. It does not parse
    provider payloads, match companies or periods, create candidates, or make
    reconciliation decisions.
    """

    normalized = _normalize_observation_values(
        provider_key=provider_key,
        provider_version=provider_version,
        parser_version=parser_version,
        provider_event_id=provider_event_id,
        raw_position=raw_position,
        cik=cik,
        ticker=ticker,
        exchange=exchange,
        provider_symbol=provider_symbol,
        company_name=company_name,
        fiscal_label_raw=fiscal_label_raw,
        fiscal_year=fiscal_year,
        period_end_date=period_end_date,
        period_type=period_type,
        fiscal_calendar_type=fiscal_calendar_type,
        period_length_weeks=period_length_weeks,
        estimated_release=estimated_release,
        estimated_release_precision=estimated_release_precision,
        release_session=release_session,
        source_observed_at=source_observed_at,
        confidence=confidence,
    )

    with transaction.atomic():
        current_source = _load_persisted_source(source)
        current_raw_record = _load_persisted_raw_data_record(raw_data_record)
        if current_raw_record.source_id != current_source.pk:
            raise InvalidEarningsCalendarObservation(
                "raw_data_record must belong to the supplied source."
            )
        if current_source.provider_adapter.strip() != normalized.provider_key:
            raise InvalidEarningsCalendarObservation(
                "provider_key must match the source provider_adapter."
            )

        try:
            with transaction.atomic():
                observation = EarningsCalendarObservation.objects.create(
                    source=current_source,
                    raw_data_record=current_raw_record,
                    **normalized.as_create_kwargs(),
                )
                return EarningsCalendarObservationWriteResult(
                    observation=observation,
                    created=True,
                )
        except IntegrityError as error:
            if not _is_observation_unique_violation(error):
                raise
            existing = EarningsCalendarObservation.objects.filter(
                raw_data_record=current_raw_record,
                parser_version=normalized.parser_version,
                provider_event_id=normalized.provider_event_id,
            ).first()
            if existing is None:
                raise
            _verify_existing_observation(
                observation=existing,
                source_id=current_source.pk,
                raw_data_record_id=current_raw_record.pk,
                normalized=normalized,
            )
            return EarningsCalendarObservationWriteResult(
                observation=existing,
                created=False,
            )


def _normalize_observation_values(
    *,
    provider_key: str,
    provider_version: str,
    parser_version: str,
    provider_event_id: str,
    raw_position: int,
    cik: str | int | None,
    ticker: str,
    exchange: str,
    provider_symbol: str,
    company_name: str,
    fiscal_label_raw: str,
    fiscal_year: int | None,
    period_end_date: date | None,
    period_type: str | None,
    fiscal_calendar_type: str | None,
    period_length_weeks: int | None,
    estimated_release: date | datetime | None,
    estimated_release_precision: str | None,
    release_session: str,
    source_observed_at: datetime | None,
    confidence: Decimal | int | float | str | None,
) -> _NormalizedObservation:
    normalized_provider_key = _normalize_required_text(
        provider_key,
        value_name="provider_key",
        maximum_length=64,
    )
    if not _PROVIDER_KEY_RE.fullmatch(normalized_provider_key):
        raise InvalidEarningsCalendarObservation(
            "provider_key must be a stable lowercase identifier."
        )

    estimated_date, estimated_at, normalized_precision = _normalize_estimated_release(
        estimated_release,
        estimated_release_precision,
    )
    return _NormalizedObservation(
        provider_key=normalized_provider_key,
        provider_version=_normalize_required_text(
            provider_version,
            value_name="provider_version",
            maximum_length=100,
        ),
        parser_version=_normalize_required_text(
            parser_version,
            value_name="parser_version",
            maximum_length=100,
        ),
        provider_event_id=_normalize_required_text(
            provider_event_id,
            value_name="provider_event_id",
            maximum_length=255,
        ),
        raw_position=_normalize_required_int(
            raw_position,
            value_name="raw_position",
            minimum=0,
        ),
        cik=_normalize_cik(cik),
        ticker=_normalize_optional_text(
            ticker,
            value_name="ticker",
            maximum_length=32,
        ),
        exchange=_normalize_optional_text(
            exchange,
            value_name="exchange",
            maximum_length=32,
        ),
        provider_symbol=_normalize_optional_text(
            provider_symbol,
            value_name="provider_symbol",
            maximum_length=64,
        ),
        company_name=_normalize_optional_text(
            company_name,
            value_name="company_name",
            maximum_length=255,
        ),
        fiscal_label_raw=_normalize_optional_text(
            fiscal_label_raw,
            value_name="fiscal_label_raw",
            maximum_length=64,
        ),
        fiscal_year=_normalize_optional_int(
            fiscal_year,
            value_name="fiscal_year",
        ),
        period_end_date=_normalize_optional_date(
            period_end_date,
            value_name="period_end_date",
        ),
        period_type=_normalize_period_type(period_type),
        fiscal_calendar_type=_normalize_fiscal_calendar_type(fiscal_calendar_type),
        period_length_weeks=_normalize_optional_int(
            period_length_weeks,
            value_name="period_length_weeks",
            minimum=1,
        ),
        estimated_release_date=estimated_date,
        estimated_release_at=estimated_at,
        estimated_release_precision=normalized_precision,
        release_session=_normalize_release_session(release_session),
        source_observed_at=_normalize_source_observed_at(source_observed_at),
        confidence=_normalize_confidence(confidence),
    )


def _load_persisted_source(source: DataSource) -> DataSource:
    if source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarObservation("source must be saved before use.")
    try:
        return DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvalidEarningsCalendarObservation("source no longer exists.") from error


def _load_persisted_raw_data_record(raw_data_record: RawDataRecord) -> RawDataRecord:
    if raw_data_record._state.adding or raw_data_record.pk is None:
        raise InvalidEarningsCalendarObservation("raw_data_record must be saved before use.")
    try:
        return RawDataRecord.objects.select_related("source").get(pk=raw_data_record.pk)
    except RawDataRecord.DoesNotExist as error:
        raise InvalidEarningsCalendarObservation("raw_data_record no longer exists.") from error


def _verify_existing_observation(
    *,
    observation: EarningsCalendarObservation,
    source_id: uuid.UUID,
    raw_data_record_id: uuid.UUID,
    normalized: _NormalizedObservation,
) -> None:
    if observation.source_id != source_id or observation.raw_data_record_id != raw_data_record_id:
        raise EarningsCalendarObservationIntegrityError(
            "An existing EarningsCalendarObservation has the same key but different lineage."
        )
    for field_name, expected in normalized.as_create_kwargs().items():
        if getattr(observation, field_name) != expected:
            raise EarningsCalendarObservationIntegrityError(
                "An existing EarningsCalendarObservation has the same key "
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


def _normalize_required_text(
    value: object,
    *,
    value_name: str,
    maximum_length: int,
) -> str:
    normalized = _normalize_optional_text(
        value,
        value_name=value_name,
        maximum_length=maximum_length,
    )
    if not normalized:
        raise InvalidEarningsCalendarObservation(f"{value_name} must not be empty.")
    return normalized


def _normalize_optional_text(
    value: object,
    *,
    value_name: str,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarObservation(f"{value_name} must be a string.")
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise InvalidEarningsCalendarObservation(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _normalize_required_int(
    value: object,
    *,
    value_name: str,
    minimum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEarningsCalendarObservation(f"{value_name} must be an integer.")
    if value < minimum:
        raise InvalidEarningsCalendarObservation(f"{value_name} must be at least {minimum}.")
    return value


def _normalize_optional_int(
    value: object,
    *,
    value_name: str,
    minimum: int | None = None,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEarningsCalendarObservation(f"{value_name} must be an integer.")
    if minimum is not None and value < minimum:
        raise InvalidEarningsCalendarObservation(f"{value_name} must be at least {minimum}.")
    return value


def _normalize_cik(value: str | int | None) -> str:
    try:
        normalized = normalize_cik(value)
    except CompanyServiceError as error:
        raise InvalidEarningsCalendarObservation(
            "cik must contain at most 10 ASCII digits."
        ) from error
    return normalized or ""


def _normalize_optional_date(value: object, *, value_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidEarningsCalendarObservation(f"{value_name} must be a date.")
    return value


def _normalize_period_type(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEarningsCalendarObservation("period_type must be a string or null.")
    normalized = value.strip().upper()
    if normalized not in ALLOWED_PERIOD_TYPES:
        raise InvalidEarningsCalendarObservation(
            "period_type must use the normalized earnings period enum."
        )
    return normalized


def _normalize_fiscal_calendar_type(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEarningsCalendarObservation("fiscal_calendar_type must be a string or null.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_FISCAL_CALENDAR_TYPES:
        raise InvalidEarningsCalendarObservation(
            "fiscal_calendar_type must use the supported calendar enum."
        )
    return normalized


def _normalize_release_session(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarObservation("release_session must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_RELEASE_SESSIONS:
        raise InvalidEarningsCalendarObservation(
            "release_session must use the supported session enum."
        )
    return normalized


def _normalize_precision(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEarningsCalendarObservation(
            "estimated_release_precision must be a string or null."
        )
    normalized = value.strip().lower()
    if normalized not in ALLOWED_EARNINGS_DATE_PRECISIONS:
        raise InvalidEarningsCalendarObservation(
            "estimated_release_precision must use the supported precision enum."
        )
    return normalized


def _normalize_estimated_release(
    value: date | datetime | None,
    precision: str | None,
) -> tuple[date | None, datetime | None, str]:
    normalized_precision = _normalize_precision(precision)
    if value is None:
        if normalized_precision not in (None, EarningsDatePrecision.UNKNOWN):
            raise InvalidEarningsCalendarObservation(
                "unknown estimated release must not declare a value precision."
            )
        return None, None, EarningsDatePrecision.UNKNOWN
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise InvalidEarningsCalendarObservation(
                "estimated_release datetime must be timezone-aware."
            )
        if normalized_precision not in (None, EarningsDatePrecision.EXACT_DATETIME):
            raise InvalidEarningsCalendarObservation(
                "estimated_release datetime requires exact_datetime precision."
            )
        return None, value, EarningsDatePrecision.EXACT_DATETIME
    if isinstance(value, date):
        if normalized_precision not in (None, EarningsDatePrecision.DATE_ONLY):
            raise InvalidEarningsCalendarObservation(
                "estimated_release date requires date_only precision."
            )
        return value, None, EarningsDatePrecision.DATE_ONLY
    raise InvalidEarningsCalendarObservation(
        "estimated_release must be a date, timezone-aware datetime, or null."
    )


def _normalize_source_observed_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise InvalidEarningsCalendarObservation(
            "source_observed_at must be a timezone-aware datetime or null."
        )
    return value


def _normalize_confidence(
    value: Decimal | int | float | str | None,
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidEarningsCalendarObservation("confidence must be a number between 0 and 1.")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise InvalidEarningsCalendarObservation(
            "confidence must be a number between 0 and 1."
        ) from error
    if not normalized.is_finite() or not Decimal("0") <= normalized <= Decimal("1"):
        raise InvalidEarningsCalendarObservation("confidence must be a number between 0 and 1.")
    return normalized.quantize(_CONFIDENCE_QUANTUM)
