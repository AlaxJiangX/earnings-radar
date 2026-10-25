from __future__ import annotations

import uuid
from io import StringIO
from typing import Any, cast

from django import forms
from django.contrib import admin, messages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import URLPattern, path

from accounts.models import User
from audit.models import AuditRecord, DataSource, SyncRun
from audit.services import record_user_action
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDateChange,
    EarningsEvent,
    EarningsReconciliationDecision,
    FilingEarningsDecision,
    FilingEarningsLink,
    InvestorRelationsDecision,
    InvestorRelationsObservation,
    ReconciliationDecisionStatus,
    ReleaseSession,
)
from earnings.services import (
    InvalidEarningsReconciliationInput,
    resolve_earnings_reconciliation_manually,
)


@admin.register(EarningsEvent)
class EarningsEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "company",
        "period_type",
        "period_end_date",
        "identity_status",
        "status",
        "includes_q4",
        "fiscal_calendar_type",
        "updated_at",
    )
    list_filter = (
        "identity_status",
        "status",
        "period_type",
        "fiscal_calendar_type",
        "includes_q4",
    )
    search_fields = ("company__display_name", "company__cik", "identity_key")
    ordering = ("-created_at",)
    readonly_fields = tuple(f.name for f in EarningsEvent._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: EarningsEvent | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: EarningsEvent | None = None) -> bool:
        return False


@admin.register(EarningsDateChange)
class EarningsDateChangeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "earnings_event",
        "field_name",
        "change_kind",
        "old_precision",
        "new_precision",
        "detected_at",
    )
    list_filter = ("field_name", "change_kind", "old_precision", "new_precision")
    search_fields = ("=earnings_event__id", "=data_change__change_key")
    ordering = ("-detected_at",)
    date_hierarchy = "detected_at"
    list_select_related = ("earnings_event", "data_change")
    readonly_fields = tuple(field.name for field in EarningsDateChange._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: EarningsDateChange | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: EarningsDateChange | None = None,
    ) -> bool:
        return False


@admin.register(FilingEarningsDecision)
class FilingEarningsDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "filing",
        "relation_type",
        "decision_type",
        "status",
        "classification",
        "decision_source",
        "decided_at",
    )
    list_filter = (
        "relation_type",
        "decision_type",
        "status",
        "classification",
        "decision_source",
    )
    search_fields = (
        "=filing__accession_number",
        "=decision_key",
        "=request_id",
    )
    ordering = ("-decided_at",)
    date_hierarchy = "decided_at"
    list_select_related = ("filing", "target_event", "actor_user", "sync_run")
    readonly_fields = tuple(field.name for field in FilingEarningsDecision._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsDecision | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsDecision | None = None,
    ) -> bool:
        return False


class SecSyncTriggerForm(forms.Form):
    """Confirmation form for one manual SEC filing sync run."""

    source = forms.ModelChoiceField(
        queryset=DataSource.objects.none(),
        empty_label=None,
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        help_text="Required. Recorded as the manual-sync audit reason.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # a fresh queryset per form instance: a class-level queryset would be
        # iterated once and then serve its cached rows for the process lifetime
        cast(
            "forms.ModelChoiceField[DataSource]", self.fields["source"]
        ).queryset = DataSource.objects.filter(
            source_type=DataSource.SourceType.SEC,
            is_enabled=True,
        ).order_by("key")


@admin.register(FilingEarningsLink)
class FilingEarningsLinkAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "filing",
        "earnings_event",
        "relation_type",
        "release_filing_classification",
        "review_status",
        "confidence",
    )
    list_filter = (
        "relation_type",
        "release_filing_classification",
        "review_status",
        "confidence",
    )
    search_fields = (
        "=filing__accession_number",
        "=earnings_event__id",
        "=current_decision__decision_key",
    )
    ordering = ("-created_at",)
    list_select_related = ("filing", "earnings_event", "current_decision", "reviewed_by")
    readonly_fields = tuple(field.name for field in FilingEarningsLink._meta.fields)
    actions = ("trigger_sec_sync",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsLink | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsLink | None = None,
    ) -> bool:
        return False

    def has_trigger_sec_sync_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("earnings.trigger_sec_sync")

    def get_urls(self) -> list[URLPattern]:
        urls = super().get_urls()
        custom = [
            path(
                "trigger-sec-sync/",
                self.admin_site.admin_view(self.trigger_sec_sync_view),
                name="earnings_filingearningslink_trigger_sec_sync",
            ),
        ]
        return custom + urls

    @admin.action(
        description="Trigger one SEC filing sync (global, requires a reason)",
        permissions=("trigger_sec_sync",),
    )
    def trigger_sec_sync(
        self,
        request: HttpRequest,
        queryset: QuerySet[FilingEarningsLink],
    ) -> HttpResponse | None:
        del queryset  # The SEC job is global; selected rows are irrelevant.
        if not request.user.has_perm("earnings.trigger_sec_sync"):
            self.message_user(
                request,
                "You do not have permission to trigger a manual SEC sync.",
                messages.ERROR,
            )
            return None
        return redirect("admin:earnings_filingearningslink_trigger_sec_sync")

    def trigger_sec_sync_view(self, request: HttpRequest) -> HttpResponse:
        actor = cast(User, request.user)
        if not request.user.has_perm("earnings.trigger_sec_sync"):
            return HttpResponseForbidden("You do not have permission to trigger a SEC sync.")
        if request.method == "POST":
            form = SecSyncTriggerForm(request.POST)
            if form.is_valid():
                source = form.cleaned_data["source"]
                idempotency_key = f"admin-manual-sec:{uuid.uuid4()}"
                output = StringIO()
                command_error: CommandError | None = None
                try:
                    call_command(
                        "sync_sec_filings",
                        source_key=source.key,
                        idempotency_key=idempotency_key,
                        stdout=output,
                    )
                except CommandError as error:
                    command_error = error
                run = SyncRun.objects.filter(idempotency_key=idempotency_key).first()
                if run is not None:
                    record_user_action(
                        actor_user=actor,
                        action=AuditRecord.Action.MANUAL_SYNC,
                        target_type=AuditRecord.TargetType.SYNC_RUN,
                        target_id=run.pk,
                        before={},
                        after={
                            "job_type": run.job_type,
                            "status": run.status,
                            "source_key": source.key,
                            "fetched_count": run.fetched_count,
                            "failed_count": run.failed_count,
                        },
                        reason=form.cleaned_data["reason"],
                        request_id=f"admin-sec-sync:{uuid.uuid4()}",
                        ip_address=request.META.get("REMOTE_ADDR"),
                    )
                if command_error is not None:
                    detail = output.getvalue().strip()
                    message = str(command_error)
                    if detail:
                        message = f"{message} {detail}"
                    form.add_error(None, message)
                else:
                    run_label = str(run.pk) if run is not None else "?"
                    run_status = run.status if run is not None else "unknown"
                    self.message_user(
                        request,
                        f"SEC sync run {run_label} finished: {run_status}.",
                        messages.SUCCESS,
                    )
                    return redirect("admin:earnings_filingearningslink_changelist")
        else:
            form = SecSyncTriggerForm()
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "title": "Trigger one SEC filing sync",
        }
        return render(request, "admin/earnings/sec_sync_trigger_form.html", context)


@admin.register(InvestorRelationsObservation)
class InvestorRelationsObservationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "company",
        "item_type",
        "period_type",
        "period_end_date",
        "source",
        "raw_position",
        "created_at",
    )
    list_filter = ("item_type", "period_type", "source")
    search_fields = (
        "=company__cik",
        "company__display_name",
        "source_event_identity",
        "=raw_data_record__content_hash",
    )
    ordering = ("-created_at",)
    list_select_related = ("company", "source", "raw_data_record")
    readonly_fields = tuple(field.name for field in InvestorRelationsObservation._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsObservation | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsObservation | None = None,
    ) -> bool:
        return False


@admin.register(InvestorRelationsDecision)
class InvestorRelationsDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "observation",
        "target_event",
        "decision_type",
        "status",
        "decided_at",
    )
    list_filter = ("decision_type", "status")
    search_fields = (
        "=decision_key",
        "=observation__id",
        "=target_event__id",
    )
    ordering = ("-decided_at",)
    list_select_related = ("observation", "target_event", "actor_user", "sync_run")
    readonly_fields = tuple(field.name for field in InvestorRelationsDecision._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsDecision | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsDecision | None = None,
    ) -> bool:
        return False


@admin.register(EarningsCalendarObservation)
class EarningsCalendarObservationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "source",
        "provider_event_id",
        "cik",
        "ticker",
        "period_type",
        "period_end_date",
        "estimated_release_at",
        "release_session",
        "created_at",
    )
    list_filter = ("source", "period_type", "release_session")
    search_fields = (
        "=cik",
        "ticker",
        "provider_event_id",
        "=raw_data_record__content_hash",
    )
    ordering = ("-created_at",)
    list_select_related = ("source", "raw_data_record")
    readonly_fields = tuple(field.name for field in EarningsCalendarObservation._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: EarningsCalendarObservation | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: EarningsCalendarObservation | None = None,
    ) -> bool:
        return False


class ManualReconciliationForm(forms.Form):
    """Reason-required manual decision on one open reconciliation."""

    DECISION_TYPES = (
        ("matched_candidate", "Matched candidate"),
        ("matched_canonical", "Matched canonical"),
        ("duplicate_of", "Duplicate of"),
        ("no_match", "No match"),
    )

    decision_type = forms.ChoiceField(choices=DECISION_TYPES)
    target_event_id = forms.UUIDField(
        required=False,
        help_text="Target EarningsEvent id; required unless the type is no_match.",
    )
    estimated_release = forms.DateField(
        required=False,
        help_text="Optional manual schedule authority (ISO date).",
    )
    release_session = forms.ChoiceField(
        required=False,
        choices=(("", "---------"),) + tuple(ReleaseSession.choices),
        help_text="Optional manual schedule authority.",
    )
    promote = forms.BooleanField(
        required=False,
        help_text="Promote the target candidate to canonical (matched_canonical only).",
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
    )


@admin.register(EarningsReconciliationDecision)
class EarningsReconciliationDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "decision_type",
        "status",
        "observation",
        "target_event",
        "actor_user",
        "decided_at",
    )
    # decision_type + status power the source-conflict queue filter
    # (open collision / conflict / review_required decisions, ADR-014).
    list_filter = ("decision_type", "status")
    search_fields = (
        "=decision_key",
        "=observation__id",
        "=target_event__id",
        "=request_id",
    )
    ordering = ("-decided_at",)
    list_select_related = ("observation", "target_event", "actor_user", "sync_run")
    readonly_fields = tuple(field.name for field in EarningsReconciliationDecision._meta.fields)
    actions = ("resolve_manually",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: EarningsReconciliationDecision | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: EarningsReconciliationDecision | None = None,
    ) -> bool:
        return False

    def has_manual_reconciliation_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("earnings.manual_reconciliation")

    def get_urls(self) -> list[URLPattern]:
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:decision_id>/resolve/",
                self.admin_site.admin_view(self.resolve_view),
                name="earnings_earningsreconciliationdecision_resolve",
            ),
        ]
        return custom + urls

    @admin.action(
        description="Resolve the selected open decision manually (requires a reason)",
        permissions=("manual_reconciliation",),
    )
    def resolve_manually(
        self,
        request: HttpRequest,
        queryset: QuerySet[EarningsReconciliationDecision],
    ) -> HttpResponse | None:
        if not request.user.has_perm("earnings.manual_reconciliation"):
            self.message_user(
                request,
                "You do not have permission to resolve reconciliations manually.",
                messages.ERROR,
            )
            return None
        if queryset.count() != 1:
            self.message_user(
                request,
                "Select exactly one open decision to resolve.",
                messages.WARNING,
            )
            return None
        decision = queryset.get()
        return redirect(
            "admin:earnings_earningsreconciliationdecision_resolve",
            decision_id=decision.pk,
        )

    def resolve_view(
        self,
        request: HttpRequest,
        decision_id: uuid.UUID,
    ) -> HttpResponse:
        actor = cast(User, request.user)
        if not request.user.has_perm("earnings.manual_reconciliation"):
            return HttpResponseForbidden(
                "You do not have permission to resolve reconciliations manually."
            )
        decision = get_object_or_404(
            EarningsReconciliationDecision.objects.select_related(
                "observation__source",
            ),
            pk=decision_id,
        )
        if decision.status != ReconciliationDecisionStatus.OPEN:
            self.message_user(
                request,
                "Only open decisions can be resolved manually.",
                messages.WARNING,
            )
            return redirect("admin:earnings_earningsreconciliationdecision_changelist")
        if request.method == "POST":
            form = ManualReconciliationForm(request.POST)
            if form.is_valid():
                decision_type = form.cleaned_data["decision_type"]
                target_event_id = form.cleaned_data["target_event_id"]
                if decision_type != "no_match" and target_event_id is None:
                    form.add_error(
                        "target_event_id",
                        "Binding manual decisions require a target event.",
                    )
                elif decision_type == "no_match" and (
                    target_event_id is not None
                    or form.cleaned_data["promote"]
                    or form.cleaned_data["estimated_release"] is not None
                    or form.cleaned_data["release_session"]
                ):
                    form.add_error(
                        None,
                        "no_match decisions cannot target, mutate, or promote an event.",
                    )
                else:
                    schedule_changes: dict[str, object] = {}
                    if form.cleaned_data["estimated_release"] is not None:
                        schedule_changes["estimated_release"] = form.cleaned_data[
                            "estimated_release"
                        ]
                    if form.cleaned_data["release_session"]:
                        schedule_changes["release_session"] = form.cleaned_data["release_session"]
                    try:
                        result = resolve_earnings_reconciliation_manually(
                            observation=decision.observation,
                            decision_type=decision_type,
                            actor_user=actor,
                            reason=form.cleaned_data["reason"],
                            request_id=f"admin-reconciliation:{uuid.uuid4()}",
                            target_event=(
                                EarningsEvent.objects.get(pk=target_event_id)
                                if target_event_id is not None
                                else None
                            ),
                            covered_fields=tuple(sorted(schedule_changes)),
                            schedule_changes=schedule_changes or None,
                            promote=form.cleaned_data["promote"],
                            ip_address=request.META.get("REMOTE_ADDR"),
                        )
                    except InvalidEarningsReconciliationInput as error:
                        form.add_error(None, str(error))
                    except EarningsEvent.DoesNotExist:
                        form.add_error("target_event_id", "Target event does not exist.")
                    else:
                        self.message_user(
                            request,
                            f"Manual decision {result.decision.decision_key[:12]}… recorded"
                            f" (created={result.decision_created}).",
                            messages.SUCCESS,
                        )
                        return redirect("admin:earnings_earningsreconciliationdecision_changelist")
        else:
            form = ManualReconciliationForm()
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "decision": decision,
            "title": f"Resolve {decision.get_decision_type_display()} decision",
        }
        return render(
            request,
            "admin/earnings/reconciliation_resolve_form.html",
            context,
        )
