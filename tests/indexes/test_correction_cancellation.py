# mypy: ignore-errors
"""Step 5 — correction/cancellation tests."""

from __future__ import annotations

from datetime import date

import pytest
from django.db import transaction

from companies.models import Company, SecurityListing
from indexes.models import IndexChangeCorrelation, IndexChangeEvent, IndexChangeLeg, MarketIndex
from indexes.services import (
    EventCorrectionLegSpec,
    InvalidIndexChangeInput,
    cancel_index_change_event,
    correct_index_change_event,
)


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def nasdaq100(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="NASDAQ100")


@pytest.fixture
def russell(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="RUSSELL2000")


@pytest.fixture
def company(db: object) -> Company:
    del db
    return Company.objects.create(
        legal_name="RevCo",
        display_name="RevCo",
        cik="0000000800",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="REV",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def listing2(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="REV2",
        exchange="NASDAQ",
        effective_from=date(2020, 1, 1),
    )


def _mk_event(company, eff_date, **kw) -> IndexChangeEvent:
    return IndexChangeEvent.objects.create(
        company=company,
        effective_date=eff_date,
        **kw,
    )


# ---- Cancellation ----


@pytest.mark.django_db
class TestCancellation:
    def test_active_to_cancelled(self, company, listing, sp500):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = cancel_index_change_event(ev)
        assert result.status_changed is True
        assert result.event.status == IndexChangeEvent.Status.CANCELLED

    def test_legs_preserved_after_cancel(self, company, listing, sp500):
        ev = _mk_event(company, date(2026, 6, 15))
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        cancel_index_change_event(ev)
        ev.refresh_from_db()
        assert ev.legs.count() == 1
        assert ev.legs.first().pk == leg.pk

    def test_cancelled_idempotent(self, company, listing, sp500):
        ev = _mk_event(company, date(2026, 6, 15))
        cancel_index_change_event(ev)
        result = cancel_index_change_event(ev)
        assert result.status_changed is False

    def test_corrected_cannot_cancel(self, company, listing, sp500):
        ev = _mk_event(company, date(2026, 6, 15))
        ev.status = IndexChangeEvent.Status.CORRECTED
        ev.save()
        with pytest.raises(InvalidIndexChangeInput, match="CORRECTED"):
            cancel_index_change_event(ev)

    def test_no_membership_mutation_on_cancel(self, company, listing, sp500):
        ev = _mk_event(company, date(2026, 6, 15))
        cancel_index_change_event(ev)
        from indexes.models import IndexMembership

        assert IndexMembership.objects.count() == 0

    def test_correlations_preserved_on_cancel(self, company, listing, sp500):
        e1 = _mk_event(company, date(2026, 6, 15))
        e2 = _mk_event(company, date(2026, 6, 18))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
        )
        cancel_index_change_event(e1)
        assert IndexChangeCorrelation.objects.filter(pk=corr.pk).exists()


# ---- Correction ----


@pytest.mark.django_db
class TestCorrection:
    def test_active_to_corrected_and_replacement(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=old,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing2,
                    index=nasdaq100,
                    action="added",
                ),
            ],
        )
        assert result.old_event.status == IndexChangeEvent.Status.CORRECTED
        assert result.new_event.status == IndexChangeEvent.Status.ACTIVE
        assert result.new_event.supersedes == old
        assert result.new_event.company == old.company
        assert result.new_event.effective_date == old.effective_date
        assert len(result.new_legs) == 1
        assert result.new_legs[0].action == IndexChangeLeg.Action.ADDED

    def test_old_legs_preserved_after_correction(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        old_leg = IndexChangeLeg.objects.create(
            event=old,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing2,
                    index=nasdaq100,
                    action="added",
                ),
            ],
        )
        old.refresh_from_db()
        assert old.legs.count() == 1
        assert old.legs.first().pk == old_leg.pk

    def test_second_correction_extends_chain(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
    ):
        e1 = _mk_event(company, date(2026, 6, 15))
        r1 = correct_index_change_event(
            e1,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        r2 = correct_index_change_event(
            r1.new_event,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing2,
                    index=nasdaq100,
                    action="removed",
                ),
            ],
        )
        assert r1.old_event.status == IndexChangeEvent.Status.CORRECTED
        assert r2.old_event.status == IndexChangeEvent.Status.CORRECTED
        assert r2.new_event.supersedes == r1.new_event
        # Chain: e1 → e2 → e3
        assert r2.new_event.supersedes.pk == r1.new_event.pk
        assert r1.new_event.supersedes.pk == e1.pk

    def test_corrected_event_cannot_be_re_corrected(
        self,
        company,
        listing,
        sp500,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        r1 = correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        with pytest.raises(InvalidIndexChangeInput, match="already CORRECTED"):
            correct_index_change_event(
                r1.old_event,  # trying to correct the now-CORRECTED event
                corrected_legs=[
                    EventCorrectionLegSpec(
                        security_listing=listing,
                        index=sp500,
                        action="removed",
                    ),
                ],
            )

    def test_old_classification_preserved(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
        russell,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=old,
            index=russell,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=old,
            index=sp500,
            security_listing=listing2,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        old.displacement = IndexChangeEvent.Displacement.UPGRADE
        old.monitoring_impact = IndexChangeEvent.MonitoringImpact.CONTINUES
        old.save()
        correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        old.refresh_from_db()
        assert old.displacement == IndexChangeEvent.Displacement.UPGRADE
        assert old.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES

    def test_replacement_starts_unclassified(
        self,
        company,
        listing,
        sp500,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        result = correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        assert result.new_event.displacement == IndexChangeEvent.Displacement.NONE
        assert result.new_event.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES

    def test_cancelled_event_cannot_be_corrected(
        self,
        company,
        listing,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        cancel_index_change_event(ev)
        ev.refresh_from_db()
        with pytest.raises(InvalidIndexChangeInput, match="CANCELLED"):
            correct_index_change_event(
                ev,
                corrected_legs=[
                    EventCorrectionLegSpec(
                        security_listing=listing,
                        index=sp500,
                        action="added",
                    ),
                ],
            )

    def test_no_membership_mutation_on_correction(
        self,
        company,
        listing,
        sp500,
    ):
        old = _mk_event(company, date(2026, 6, 15))
        correct_index_change_event(
            old,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        from indexes.models import IndexMembership

        assert IndexMembership.objects.count() == 0

    def test_old_correlations_preserved_on_correction(
        self,
        company,
        listing,
        sp500,
    ):
        e1 = _mk_event(company, date(2026, 6, 15))
        e2 = _mk_event(company, date(2026, 6, 18))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
        )
        correct_index_change_event(
            e1,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        assert IndexChangeCorrelation.objects.filter(pk=corr.pk).exists()

    def test_rollback_keeps_old_active_on_leg_failure(
        self,
        company,
        listing,
        sp500,
        nasdaq100,
    ):
        """If leg creation fails, old event must stay ACTIVE."""
        old = _mk_event(company, date(2026, 6, 15))
        with pytest.raises(InvalidIndexChangeInput):
            with transaction.atomic():
                correct_index_change_event(
                    old,
                    corrected_legs=[
                        EventCorrectionLegSpec(
                            security_listing=listing,
                            index=sp500,
                            action="invalid_action_name",
                        ),
                    ],
                )
        old.refresh_from_db()
        assert old.status == IndexChangeEvent.Status.ACTIVE
