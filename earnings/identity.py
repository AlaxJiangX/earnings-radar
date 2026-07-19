"""Deterministic helpers for EarningsEvent identity and period normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import UUID

IDENTITY_RULE_VERSION = "v1"


def derive_earnings_identity_key(
    *,
    company_id: UUID,
    period_end_date: date,
    period_type: str,
) -> str:
    """Return a deterministic SHA-256 canonical identity key.

    The identity is based solely on the business identity fields from
    ADR-001: company + period_end_date + period_type.

    identity_rule_version is NOT included in the hash input — it is
    stored separately as provenance metadata.
    """
    identity = {
        "c": str(company_id),
        "d": period_end_date.isoformat(),
        "p": period_type,
    }
    serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def normalize_period_type(raw_label: str) -> tuple[str | None, bool]:
    """Convert a raw period label to canonical (period_type, includes_q4).

    Returns (None, False) for unrecognized labels — the caller must
    handle unknown labels explicitly.  Unrecognized labels are NOT
    automatically mapped to OTHER.
    """
    label = raw_label.strip().upper()

    if label in ("Q1",):
        return ("Q1", False)
    if label in ("Q2",):
        return ("Q2", False)
    if label in ("Q3",):
        return ("Q3", False)
    if label in ("Q4",):
        return ("FY", True)
    if label in ("FY", "ANNUAL", "YEAR", "FULL_YEAR"):
        return ("FY", True)
    if label in ("H1",):
        return ("H1", False)
    if label in ("H2",):
        return ("H2", False)
    if label in ("OTHER",):
        return ("OTHER", False)

    return (None, False)
