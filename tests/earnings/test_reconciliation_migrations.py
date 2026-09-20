import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("earnings", "0004_earningscalendarobservation")
MIGRATE_TO = ("earnings", "0005_earningsreconciliationdecision")


@pytest.mark.django_db(transaction=True)
def test_0005_creates_decision_schema_and_reverses() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        with pytest.raises(LookupError):
            old_apps.get_model("earnings", "EarningsReconciliationDecision")

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps
        Decision = new_apps.get_model(
            "earnings",
            "EarningsReconciliationDecision",
        )

        assert Decision._meta.db_table == "earnings_earningsreconciliationdecision"
        assert len(Decision._meta.indexes) == 3
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                Decision._meta.db_table,
            )
        expected_constraints = {
            "earnings_reconciliation_decision_key_unique",
            "earnings_reconciliation_decision_type_valid",
            "earnings_reconciliation_decision_status_valid",
            "earnings_reconciliation_decision_outcome_valid",
            "earnings_reconciliation_decision_context_valid",
            "earnings_reconciliation_decision_key_valid",
            "earnings_reconciliation_decision_rule_not_empty",
            "earnings_reconciliation_decision_not_self",
        }
        assert expected_constraints.issubset(constraints)

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        reverted_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        with pytest.raises(LookupError):
            reverted_apps.get_model("earnings", "EarningsReconciliationDecision")

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        reapplied_apps = executor.loader.project_state([MIGRATE_TO]).apps
        assert reapplied_apps.get_model(
            "earnings",
            "EarningsReconciliationDecision",
        )
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
