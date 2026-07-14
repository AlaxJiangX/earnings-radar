from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from audit.models import AuditRecord, DataChange
from indexes.services import InvalidMarketIndexCode, set_index_enabled

if TYPE_CHECKING:
    from accounts.models import User


class TestSetIndexEnabled:
    def test_disable_enabled_index(self, db: object, user: User) -> None:
        del db
        result = set_index_enabled(
            code="SP500",
            enabled=False,
            actor_user=user,
            reason="Testing disable",
            request_id=str(uuid.uuid4()),
        )
        assert result.changed is True
        assert result.enabled is False
        assert result.index.is_enabled is False
        assert len(result.data_changes) == 1
        assert result.data_changes[0].created is True
        assert result.data_changes[0].change is not None
        assert result.data_changes[0].change.field_name == "is_enabled"
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.UPDATE

    def test_enable_disabled_index(self, db: object, user: User) -> None:
        del db
        set_index_enabled(
            code="SP500",
            enabled=False,
            actor_user=user,
            reason="first",
            request_id=str(uuid.uuid4()),
        )
        result = set_index_enabled(
            code="SP500",
            enabled=True,
            actor_user=user,
            reason="Testing enable",
            request_id=str(uuid.uuid4()),
        )
        assert result.changed is True
        assert result.enabled is True
        assert result.index.is_enabled is True

    def test_idempotent_same_value(self, db: object, user: User) -> None:
        del db
        request_id = str(uuid.uuid4())
        first = set_index_enabled(
            code="SP500",
            enabled=False,
            actor_user=user,
            reason="first",
            request_id=request_id,
        )
        assert first.changed is True

        second = set_index_enabled(
            code="SP500",
            enabled=False,
            actor_user=user,
            reason="second",
            request_id=request_id,
        )
        assert second.changed is False
        assert second.data_changes == ()
        assert second.audit_record is None

    def test_creates_exactly_one_data_change(self, db: object, user: User) -> None:
        del db
        request_id = str(uuid.uuid4())
        result = set_index_enabled(
            code="NASDAQ100",
            enabled=False,
            actor_user=user,
            reason="count test",
            request_id=request_id,
        )
        assert result.changed is True
        dc_count = DataChange.objects.filter(
            target_type=DataChange.TargetType.MARKET_INDEX,
            target_id=result.index.pk,
        ).count()
        assert dc_count == 1

    def test_creates_exactly_one_audit_record(self, db: object, user: User) -> None:
        del db
        request_id = str(uuid.uuid4())
        result = set_index_enabled(
            code="DJIA",
            enabled=False,
            actor_user=user,
            reason="audit test",
            request_id=request_id,
        )
        assert result.changed is True
        ar_count = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.MARKET_INDEX,
            target_id=result.index.pk,
        ).count()
        assert ar_count == 1

    def test_correct_data_change_values(self, db: object, user: User) -> None:
        del db
        request_id = str(uuid.uuid4())
        result = set_index_enabled(
            code="RUSSELL2000",
            enabled=False,
            actor_user=user,
            reason="value test",
            request_id=request_id,
        )
        assert result.changed is True
        dc = result.data_changes[0].change
        assert dc is not None
        assert dc.old_value is True
        assert dc.new_value is False
        assert dc.target_type == DataChange.TargetType.MARKET_INDEX
        assert dc.field_name == "is_enabled"

    def test_correct_audit_record_values(self, db: object, user: User) -> None:
        del db
        request_id = str(uuid.uuid4())
        result = set_index_enabled(
            code="SP500",
            enabled=False,
            actor_user=user,
            reason="audit value test",
            request_id=request_id,
        )
        assert result.audit_record is not None
        ar = result.audit_record
        assert ar.action == AuditRecord.Action.UPDATE
        assert ar.target_type == AuditRecord.TargetType.MARKET_INDEX
        assert ar.target_id == result.index.pk
        assert ar.before == {"is_enabled": True}
        assert ar.after == {"is_enabled": False}
        assert ar.reason == "audit value test"
        assert ar.request_id == request_id

    def test_invalid_code_raises(self, db: object, user: User) -> None:
        del db
        with pytest.raises(InvalidMarketIndexCode):
            set_index_enabled(
                code="INVALID",
                enabled=False,
                actor_user=user,
                reason="test",
                request_id=str(uuid.uuid4()),
            )

    def test_unknown_code_raises_invalid_code(self, db: object, user: User) -> None:
        del db
        with pytest.raises(InvalidMarketIndexCode):
            set_index_enabled(
                code="FTSE100",
                enabled=False,
                actor_user=user,
                reason="test",
                request_id=str(uuid.uuid4()),
            )


class TestGetIndexByCode:
    def test_get_existing(self, db: object) -> None:
        del db
        from indexes.services import get_index_by_code

        index = get_index_by_code(code="SP500")
        assert index.code == "SP500"

    def test_get_with_invalid_code_format(self, db: object) -> None:
        del db
        from indexes.services import get_index_by_code

        with pytest.raises(InvalidMarketIndexCode):
            get_index_by_code(code="NONEXISTENT")
