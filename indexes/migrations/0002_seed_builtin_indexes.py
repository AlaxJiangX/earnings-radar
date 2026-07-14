from __future__ import annotations

from typing import Any

from django.db import migrations

BUILTIN_INDEXES = (
    ("SP500", "S&P 500", "LARGE"),
    ("NASDAQ100", "Nasdaq 100", "LARGE"),
    ("DJIA", "Dow Jones Industrial Average", "LARGE"),
    ("RUSSELL2000", "Russell 2000", "SMALL"),
)


def seed_builtin_indexes(apps: Any, schema_editor: Any) -> None:
    del schema_editor
    MarketIndex = apps.get_model("indexes", "MarketIndex")

    for code, name, index_group in BUILTIN_INDEXES:
        existing = MarketIndex.objects.filter(code=code).first()
        if existing is not None:
            if existing.name != name or existing.index_group != index_group:
                raise ValueError(
                    f"Built-in index {code} exists but has inconsistent name or group. "
                    f"Expected name={name!r} group={index_group!r}; "
                    f"found name={existing.name!r} group={existing.index_group!r}."
                )
            continue
        MarketIndex.objects.create(
            code=code,
            name=name,
            index_group=index_group,
            is_enabled=True,
        )


def remove_builtin_indexes(apps: Any, schema_editor: Any) -> None:
    del schema_editor
    MarketIndex = apps.get_model("indexes", "MarketIndex")
    MarketIndex.objects.filter(code__in=[c for c, _, _ in BUILTIN_INDEXES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("indexes", "0001_initial_market_index"),
    ]

    operations = [
        migrations.RunPython(
            code=seed_builtin_indexes,
            reverse_code=remove_builtin_indexes,
        ),
    ]
