# mypy: ignore-errors
"""ADR-021 metadata-only release classification decision table."""

from __future__ import annotations

import pytest

from earnings.services.filing_links import (
    REASON_ITEM_202_WITH_EARNINGS_EXHIBIT,
    REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT,
    REASON_ITEMS_METADATA_MISSING,
    REASON_NO_ITEM_202,
    REASON_SIX_K_REQUIRES_REVIEW,
    REASON_UNSUPPORTED_EXHIBIT_ONLY,
    InvalidFilingEarningsInput,
    classify_release_filing,
)


@pytest.mark.parametrize(
    ("form_type", "reported_items", "document_types", "classification", "reason"),
    (
        ("8-K", "2.02", ("EX-99.1",), "YES", REASON_ITEM_202_WITH_EARNINGS_EXHIBIT),
        ("8-K", "2.02,9.01", ("ex-99",), "YES", REASON_ITEM_202_WITH_EARNINGS_EXHIBIT),
        (
            "8-K",
            "9.01",
            ("EX-99.1",),
            "NO",
            REASON_NO_ITEM_202,
        ),
        ("8-K", "9.01", (), "NO", REASON_NO_ITEM_202),
        ("8-K", "", ("EX-99.1",), "REVIEW_REQUIRED", REASON_ITEMS_METADATA_MISSING),
        ("8-K", "2.02,bad", (), "REVIEW_REQUIRED", REASON_ITEMS_METADATA_MISSING),
        (
            "8-K",
            "2.02",
            (),
            "REVIEW_REQUIRED",
            REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT,
        ),
        (
            "8-K",
            "2.02",
            ("EX-99.2",),
            "REVIEW_REQUIRED",
            REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT,
        ),
        (
            "8-K",
            "9.01",
            ("EX-99.2",),
            "REVIEW_REQUIRED",
            REASON_UNSUPPORTED_EXHIBIT_ONLY,
        ),
        (
            "8-K",
            "2.02",
            ("EX-99.1", "EX-99.2"),
            "YES",
            REASON_ITEM_202_WITH_EARNINGS_EXHIBIT,
        ),
        (
            "8-K",
            "9.01",
            ("EX-99.1", "EX-99.2"),
            "NO",
            REASON_NO_ITEM_202,
        ),
        (
            "6-K",
            "2.02",
            ("EX-99.1",),
            "REVIEW_REQUIRED",
            REASON_SIX_K_REQUIRES_REVIEW,
        ),
    ),
)
def test_classification_decision_table(
    form_type: str,
    reported_items: str,
    document_types: tuple[str, ...],
    classification: str,
    reason: str,
) -> None:
    result = classify_release_filing(
        form_type=form_type,
        reported_items=reported_items,
        document_types=document_types,
    )
    assert result.classification == classification
    assert result.reason_code == reason


def test_classification_never_uses_filename_or_description() -> None:
    result = classify_release_filing(
        form_type="8-K",
        reported_items="9.01",
        document_types=("text/html",),
    )
    assert result.classification == "NO"
    assert result.supported_exhibits == ()
    assert result.unsupported_exhibits == ()


def test_item_precedence_beats_unsupported_exhibit_reason() -> None:
    result = classify_release_filing(
        form_type="8-K",
        reported_items="2.02",
        document_types=("EX-99.3",),
    )
    assert result.reason_code == REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT


def test_non_release_form_is_rejected() -> None:
    with pytest.raises(InvalidFilingEarningsInput):
        classify_release_filing(
            form_type="10-Q",
            reported_items="2.02",
            document_types=(),
        )
