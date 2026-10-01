# mypy: ignore-errors
"""Model / DB constraint / append-only / admin tests for Stage 4.5B IR schema."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction

from audit.models import (
    AppendOnlyRecordError,
    AuditRecord,
    AuditRecordTargetType,
    DomainTargetType,
    RawDataObservation,
    SyncRun,
)
from audit.services import record_raw_data_observation
from earnings.admin import InvestorRelationsDecisionAdmin, InvestorRelationsObservationAdmin
from earnings.models import (
    InvestorRelationsDecision,
    InvestorRelationsObservation,
)
from tests.earnings.helpers import make_event
from tests.earnings.ir_helpers import (
    DEFAULT_IR_SOURCE_KEY,
    DEFAULT_IR_SOURCE_URL_BASE,
    IR_FETCHED_AT,
    ir_item,
    ir_payload,
    make_ir_company,
    make_ir_source,
    make_ir_sync_run,
)

pytestmark = pytest.mark.django_db


def _setup() -> tuple[object, object, SyncRun, object, object]:
    company = make_ir_company("model")
    source = make_ir_source("model")
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    run = make_ir_sync_run(company=company, source=source, request_id="ir-model")
    payload = ir_payload([ir_item(company, confirmed_release="2026-10-20")])
    raw = record_raw_data_observation(
        sync_run=run,
        source_url=f"{DEFAULT_IR_SOURCE_URL_BASE}/{DEFAULT_IR_SOURCE_KEY}/{company.pk}",
        payload=payload,
        fetched_at=IR_FETCHED_AT,
        observed_at=IR_FETCHED_AT,
    )
    return company, source, run, event, raw


def _valid_observation_kwargs(
    *,
    source: object,
    raw_data_record: object,
    company: object,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "source": source,
        "raw_data_record": raw_data_record,
        "company": company,
        "provider_key": "fixture-ir",
        "provider_version": "fixture-v1",
        "parser_version": "fixture-ir-parser-v1",
        "source_event_identity": f"native-{uuid.uuid4().hex[:8]}",
        "raw_position": 1,
        "period_end_date": date(2026, 9, 30),
        "period_type": "Q3",
        "item_type": "release_confirmation",
        "confirmed_release_date": date(2026, 10, 20),
        "confirmed_release_precision": "date_only",
        "estimated_release_precision": "unknown",
        "earnings_release_precision": "unknown",
        "conference_call_precision": "unknown",
    }
    values.update(overrides)
    return values


def test_observation_unique_identity_is_enforced() -> None:
    company, source, _run, _event, raw = _setup()
    first = InvestorRelationsObservation.objects.create(
        **_valid_observation_kwargs(
            source=source,
            raw_data_record=raw.record,
            company=company,
        )
    )

    with pytest.raises(IntegrityError) as exc_info:
        with transaction.atomic():
            InvestorRelationsObservation.objects.create(
                **_valid_observation_kwargs(
                    source=source,
                    raw_data_record=raw.record,
                    company=company,
                    source_event_identity=first.source_event_identity,
                )
            )

    constraint = getattr(getattr(exc_info.value.__cause__, "diag", None), "constraint_name", None)
    assert constraint == "investor_relations_observation_record_parser_event_unique"


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    (
        (
            {
                "item_type": "release_confirmation",
                "confirmed_release_date": None,
                "confirmed_release_precision": "unknown",
                "estimated_release_precision": "unknown",
            },
            "investor_relations_observation_confirmation_shape_valid",
        ),
        (
            {
                "item_type": "results_release",
                "earnings_release_precision": "unknown",
            },
            "investor_relations_observation_results_shape_valid",
        ),
        (
            {
                "item_type": "call_notice",
                "confirmed_release_date": None,
                "confirmed_release_precision": "unknown",
                "estimated_release_precision": "unknown",
                "conference_call_precision": "unknown",
            },
            "investor_relations_observation_call_shape_valid",
        ),
        (
            {
                "item_type": "cancellation",
                "cancellation": None,
                "confirmed_release_date": None,
                "confirmed_release_precision": "unknown",
            },
            "investor_relations_observation_cancellation_shape_valid",
        ),
    ),
)
def test_observation_item_shape_constraints(
    overrides: dict[str, object],
    constraint_name: str,
) -> None:
    company, source, _run, _event, raw = _setup()

    with pytest.raises(IntegrityError) as exc_info:
        with transaction.atomic():
            InvestorRelationsObservation.objects.create(
                **_valid_observation_kwargs(
                    source=source,
                    raw_data_record=raw.record,
                    company=company,
                    **overrides,
                )
            )

    constraint = getattr(getattr(exc_info.value.__cause__, "diag", None), "constraint_name", None)
    assert constraint == constraint_name


def test_observation_rejects_foreign_internal_identity_namespace() -> None:
    company, source, _run, _event, raw = _setup()

    with pytest.raises(IntegrityError) as exc_info:
        with transaction.atomic():
            InvestorRelationsObservation.objects.create(
                **_valid_observation_kwargs(
                    source=source,
                    raw_data_record=raw.record,
                    company=company,
                    source_event_identity="internal:v2:" + "a" * 64,
                )
            )

    constraint = getattr(getattr(exc_info.value.__cause__, "diag", None), "constraint_name", None)
    assert constraint == "investor_relations_observation_internal_namespace_valid"


def test_observation_is_append_only() -> None:
    company, source, _run, _event, raw = _setup()
    observation = InvestorRelationsObservation.objects.create(
        **_valid_observation_kwargs(
            source=source,
            raw_data_record=raw.record,
            company=company,
        )
    )

    observation.raw_position = 2
    with pytest.raises(AppendOnlyRecordError):
        observation.save()
    with pytest.raises(AppendOnlyRecordError):
        InvestorRelationsObservation.objects.filter(pk=observation.pk).update(raw_position=2)
    with pytest.raises(AppendOnlyRecordError):
        InvestorRelationsObservation.objects.filter(pk=observation.pk).delete()


def _valid_decision_kwargs(
    *,
    observation: InvestorRelationsObservation,
    event: object = None,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "observation": observation,
        "decision_type": "confirmed_schedule",
        "status": "resolved",
        "covered_fields": ["confirmed_release"],
        "rule_version": "fixture-ir-decision-v1",
        "match_factors": {},
        "reason": "FIXTURE",
        "target_event": event,
        "source_raw_data_record": observation.raw_data_record,
        "sync_run": None,
        "decision_key": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    }
    values.update(overrides)
    return values


def _make_observation() -> tuple[InvestorRelationsObservation, object, SyncRun]:
    company, source, run, event, raw = _setup()
    observation = InvestorRelationsObservation.objects.create(
        **_valid_observation_kwargs(
            source=source,
            raw_data_record=raw.record,
            company=company,
        )
    )
    return observation, event, run


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    (
        (
            {"decision_type": "confirmed_schedule", "status": "resolved", "target_event": None},
            "investor_relations_decision_outcome_valid",
        ),
        (
            {"decision_type": "no_match", "status": "rejected", "target_event": "event"},
            "investor_relations_decision_outcome_valid",
        ),
        (
            {"decision_type": "conflict", "status": "resolved"},
            "investor_relations_decision_outcome_valid",
        ),
        (
            {"sync_run": None, "request_id": ""},
            "investor_relations_decision_context_valid",
        ),
    ),
)
def test_decision_outcome_and_context_constraints(
    overrides: dict[str, object],
    constraint_name: str,
) -> None:
    observation, event, run = _make_observation()
    values = _valid_decision_kwargs(observation=observation, event=event)
    values["sync_run"] = run
    for key, value in overrides.items():
        values[key] = event if value == "event" else value

    with pytest.raises(IntegrityError) as exc_info:
        with transaction.atomic():
            InvestorRelationsDecision.objects.create(**values)

    constraint = getattr(getattr(exc_info.value.__cause__, "diag", None), "constraint_name", None)
    assert constraint == constraint_name


def test_decision_allows_ignored_with_target_event() -> None:
    observation, event, run = _make_observation()
    values = _valid_decision_kwargs(observation=observation, event=event)
    values["sync_run"] = run
    values["decision_type"] = "ignored"
    values["status"] = "rejected"
    values["covered_fields"] = []

    decision = InvestorRelationsDecision.objects.create(**values)

    assert decision.target_event_id == event.pk


def test_decision_is_append_only_and_key_is_unique() -> None:
    observation, event, run = _make_observation()
    values = _valid_decision_kwargs(observation=observation, event=event)
    values["sync_run"] = run
    decision = InvestorRelationsDecision.objects.create(**values)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InvestorRelationsDecision.objects.create(**values)
    with pytest.raises(AppendOnlyRecordError):
        decision.save()
    with pytest.raises(AppendOnlyRecordError):
        InvestorRelationsDecision.objects.filter(pk=decision.pk).delete()


def test_audit_target_enums_and_db_constraint_accept_ir_targets() -> None:
    assert DomainTargetType.INVESTOR_RELATIONS_OBSERVATION.value == "investor_relations_observation"
    assert DomainTargetType.INVESTOR_RELATIONS_DECISION.value == "investor_relations_decision"
    assert (
        AuditRecordTargetType.INVESTOR_RELATIONS_OBSERVATION.value
        == "investor_relations_observation"
    )
    _company, _source, run, _event, _raw = _setup()
    record = AuditRecord.objects.create(
        sync_run=run,
        action="update",
        target_type="investor_relations_decision",
        target_id=uuid.uuid4(),
        request_id="ir-target-test",
        audit_key=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    assert record.target_type == "investor_relations_decision"


def test_ir_admin_is_read_only() -> None:
    observation_admin = admin.site._registry[InvestorRelationsObservation]
    decision_admin = admin.site._registry[InvestorRelationsDecision]

    assert isinstance(observation_admin, InvestorRelationsObservationAdmin)
    assert isinstance(decision_admin, InvestorRelationsDecisionAdmin)
    assert observation_admin.has_add_permission(request=None) is False  # type: ignore[arg-type]
    assert observation_admin.has_change_permission(request=None) is False  # type: ignore[arg-type]
    assert observation_admin.has_delete_permission(request=None) is False  # type: ignore[arg-type]
    assert decision_admin.has_add_permission(request=None) is False  # type: ignore[arg-type]
    assert decision_admin.has_change_permission(request=None) is False  # type: ignore[arg-type]
    assert decision_admin.has_delete_permission(request=None) is False  # type: ignore[arg-type]


def test_ir_observation_rejects_raw_record_from_another_source() -> None:
    company, source, _run, _event, raw = _setup()
    other_source = make_ir_source("model-other")
    assert not RawDataObservation.objects.filter(
        raw_data_record=raw.record,
        sync_run__source=other_source,
    ).exists()

    from earnings.services import InvalidInvestorRelationsObservation
    from earnings.services.ir_observation import record_investor_relations_observation

    with pytest.raises(InvalidInvestorRelationsObservation):
        record_investor_relations_observation(
            source=other_source,
            raw_data_record=raw.record,
            company=company,
            provider_key="fixture-ir",
            provider_version="fixture-v1",
            parser_version="fixture-ir-parser-v1",
            source_event_identity=f"native-{uuid.uuid4().hex[:8]}",
            raw_position=1,
            period_end_date=date(2026, 9, 30),
            period_type="Q3",
            item_type="release_confirmation",
            confirmed_release=date(2026, 10, 20),
        )
    assert source.pk is not None
