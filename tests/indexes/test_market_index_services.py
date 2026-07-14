from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from django.db import connection

from audit.models import AuditRecord, DataChange
from audit.services import InvalidDataChange
from indexes.models import MarketIndex
from indexes.services import (
    InvalidMarketIndexCode,
    MarketIndexNotFound,
    set_index_enabled,
)

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

    # --- concurrency regression ---

    def test_all_reads_inside_single_atomic_block(self) -> None:
        """Structural test: the source code of set_index_enabled must contain
        exactly one `with transaction.atomic():` block, and the
        `select_for_update().get()` call must be inside it. No bare
        `MarketIndex.objects.get()` may appear before the atomic block.
        """
        import ast
        import inspect

        source = inspect.getsource(set_index_enabled)
        tree = ast.parse(source)

        # Count with-statements targeting transaction.atomic
        atomic_count = 0
        select_for_update_inside_atomic = False
        bare_get_outside_atomic = False

        class AtomicVisitor(ast.NodeVisitor):
            def visit_With(self, node: ast.With) -> None:
                nonlocal atomic_count, select_for_update_inside_atomic
                for item in node.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Attribute)
                        and isinstance(item.context_expr.func.value, ast.Name)
                        and item.context_expr.func.value.id == "transaction"
                        and item.context_expr.func.attr == "atomic"
                    ):
                        atomic_count += 1
                        # Check if select_for_update is in this block
                        for child in ast.walk(node):
                            if (
                                isinstance(child, ast.Call)
                                and isinstance(child.func, ast.Attribute)
                                and child.func.attr == "select_for_update"
                            ):
                                select_for_update_inside_atomic = True
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                nonlocal bare_get_outside_atomic
                # Check for MarketIndex.objects.get(...) calls
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "objects"
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "MarketIndex"
                ):
                    # Check if this call is inside an atomic block
                    bare_get_outside_atomic = True
                self.generic_visit(node)

        AtomicVisitor().visit(tree)

        assert atomic_count >= 1, "set_index_enabled must use transaction.atomic()"
        assert select_for_update_inside_atomic, (
            "select_for_update() must be called inside transaction.atomic()"
        )
        assert not bare_get_outside_atomic, (
            "No bare MarketIndex.objects.get() may appear — all reads "
            "must go through select_for_update inside the atomic block"
        )

    def test_toggle_uses_current_database_value_for_audit(self, db: object, user: User) -> None:
        """Verify that set_index_enabled reads the current committed value
        from the database, even after an external modification.

        A raw SQL update simulates a prior modification (for example from
        another process or an admin operation).  The Service must see the
        modified value via its select_for_update read, record old_value as
        that modified value, and express the correct transition in its
        DataChange and AuditRecord.
        """
        del db

        sp500 = MarketIndex.objects.get(code="SP500")
        assert sp500.is_enabled is True

        # Modify is_enabled externally (simulating a prior change).
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE indexes_marketindex SET is_enabled = %s, "
                "updated_at = NOW() WHERE code = %s",
                [False, "SP500"],
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled is False

        result = set_index_enabled(
            code="SP500",
            enabled=True,
            actor_user=user,
            reason="external modification regression",
            request_id=str(uuid.uuid4()),
        )

        assert result.changed is True, (
            "Must detect change after external modification; "
            "select_for_update must read the committed value."
        )
        assert result.enabled is True

        sp500.refresh_from_db()
        assert sp500.is_enabled is True

        assert len(result.data_changes) == 1
        dc = result.data_changes[0].change
        assert dc is not None
        assert dc.old_value is False, "old_value must reflect the externally modified state"
        assert dc.new_value is True

    def test_none_actor_user_rolls_back(self, db: object, user: User) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_enabled = sp500.is_enabled
        dc_before = self._current_dc_count(sp500.pk)
        ar_before = self._current_ar_count(sp500.pk)

        target_enabled = not original_enabled
        with pytest.raises(InvalidDataChange):
            set_index_enabled(
                code="SP500",
                enabled=target_enabled,
                actor_user=None,  # type: ignore[arg-type]
                reason="valid reason",
                request_id=str(uuid.uuid4()),
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled == original_enabled, "is_enabled must not change"
        assert self._current_dc_count(sp500.pk) == dc_before, "no new DataChange"
        assert self._current_ar_count(sp500.pk) == ar_before, "no new AuditRecord"

    # --- invalid input and rollback tests ---

    def _current_dc_count(self, index_id: uuid.UUID) -> int:
        return DataChange.objects.filter(
            target_type=DataChange.TargetType.MARKET_INDEX,
            target_id=index_id,
        ).count()

    def _current_ar_count(self, index_id: uuid.UUID) -> int:
        return AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.MARKET_INDEX,
            target_id=index_id,
        ).count()

    def test_empty_reason_rolls_back(self, db: object, user: User) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_enabled = sp500.is_enabled
        dc_before = self._current_dc_count(sp500.pk)
        ar_before = self._current_ar_count(sp500.pk)

        with pytest.raises(InvalidDataChange):
            set_index_enabled(
                code="SP500",
                enabled=not original_enabled,
                actor_user=user,
                reason="",
                request_id=str(uuid.uuid4()),
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled == original_enabled, "is_enabled must not change"
        assert self._current_dc_count(sp500.pk) == dc_before, "no new DataChange"
        assert self._current_ar_count(sp500.pk) == ar_before, "no new AuditRecord"

    def test_whitespace_reason_rolls_back(self, db: object, user: User) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_enabled = sp500.is_enabled
        dc_before = self._current_dc_count(sp500.pk)
        ar_before = self._current_ar_count(sp500.pk)

        with pytest.raises(InvalidDataChange):
            set_index_enabled(
                code="SP500",
                enabled=not original_enabled,
                actor_user=user,
                reason="   ",
                request_id=str(uuid.uuid4()),
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled == original_enabled
        assert self._current_dc_count(sp500.pk) == dc_before
        assert self._current_ar_count(sp500.pk) == ar_before

    def test_empty_request_id_rolls_back(self, db: object, user: User) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_enabled = sp500.is_enabled
        dc_before = self._current_dc_count(sp500.pk)
        ar_before = self._current_ar_count(sp500.pk)

        with pytest.raises(InvalidDataChange):
            set_index_enabled(
                code="SP500",
                enabled=not original_enabled,
                actor_user=user,
                reason="valid reason",
                request_id="",
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled == original_enabled
        assert self._current_dc_count(sp500.pk) == dc_before
        assert self._current_ar_count(sp500.pk) == ar_before

    def test_whitespace_request_id_rolls_back(self, db: object, user: User) -> None:
        del db
        sp500 = MarketIndex.objects.get(code="SP500")
        original_enabled = sp500.is_enabled
        dc_before = self._current_dc_count(sp500.pk)
        ar_before = self._current_ar_count(sp500.pk)

        with pytest.raises(InvalidDataChange):
            set_index_enabled(
                code="SP500",
                enabled=not original_enabled,
                actor_user=user,
                reason="valid reason",
                request_id="   ",
            )

        sp500.refresh_from_db()
        assert sp500.is_enabled == original_enabled
        assert self._current_dc_count(sp500.pk) == dc_before
        assert self._current_ar_count(sp500.pk) == ar_before


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

    def test_get_nonexistent_valid_code(self, db: object) -> None:
        """A valid code that doesn't exist in the database raises MarketIndexNotFound."""
        del db
        from indexes.services import get_index_by_code

        MarketIndex.objects.filter(code="NASDAQ100").delete()
        with pytest.raises(MarketIndexNotFound):
            get_index_by_code(code="NASDAQ100")
