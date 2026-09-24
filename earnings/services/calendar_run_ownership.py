"""PostgreSQL session ownership for earnings-calendar window execution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any
from uuid import UUID

from django.db import connections

from audit.models import SyncRun

_CURRENT_LEASE: ContextVar[CalendarRunLease | None] = ContextVar(
    "earnings_calendar_run_lease", default=None
)


class EarningsCalendarRunBusy(RuntimeError):
    """Another database session owns this source and job type."""


class EarningsCalendarRunOwnershipLost(RuntimeError):
    """The database session holding the advisory lock was replaced."""


@dataclass(frozen=True, slots=True)
class CalendarRunLease:
    source_id: UUID
    job_type: str
    lock_key: int
    session: object
    database_alias: str

    def assert_active(self) -> None:
        database = connections[self.database_alias]
        if database.connection is not self.session:
            raise EarningsCalendarRunOwnershipLost(
                "Earnings calendar ownership connection was lost during execution."
            )


def _lock_key(*, source_id: UUID, job_type: str) -> int:
    identity = f"earnings-calendar-window:v1:{source_id}:{job_type}".encode()
    digest = hashlib.sha256(identity).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@contextmanager
def calendar_run_ownership(
    *, source_id: UUID, job_type: str, database_alias: str = "default"
) -> Iterator[CalendarRunLease]:
    """Acquire a fail-fast session lock across short transactions and page fetches."""

    database = connections[database_alias]
    if database.vendor != "postgresql":
        raise RuntimeError("Earnings calendar ownership requires PostgreSQL.")
    key = _lock_key(source_id=source_id, job_type=job_type)
    with database.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        raise EarningsCalendarRunBusy("An earnings calendar run already owns this source and job.")

    lease = CalendarRunLease(
        source_id=source_id,
        job_type=job_type,
        lock_key=key,
        session=database.connection,
        database_alias=database_alias,
    )
    token = _CURRENT_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_LEASE.reset(token)
        lease.assert_active()
        with database.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
            released = bool(cursor.fetchone()[0])
        if not released:
            raise EarningsCalendarRunOwnershipLost(
                "Earnings calendar ownership lock was already released."
            )


def assert_calendar_run_ownership(
    *,
    source_id: UUID | None = None,
    job_type: str | None = None,
) -> None:
    lease = _CURRENT_LEASE.get()
    if lease is None:
        raise EarningsCalendarRunOwnershipLost("Earnings calendar run has no ownership lease.")
    lease.assert_active()
    if source_id is not None and lease.source_id != source_id:
        raise EarningsCalendarRunOwnershipLost(
            "Earnings calendar ownership lease belongs to a different DataSource."
        )
    if job_type is not None and lease.job_type != job_type:
        raise EarningsCalendarRunOwnershipLost(
            "Earnings calendar ownership lease belongs to a different job type."
        )


def owned_calendar_run[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Protect the existing caller-owned pagination entry point."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        sync_run: Any = kwargs.get("sync_run")
        if not isinstance(sync_run, SyncRun) or sync_run.pk is None or sync_run._state.adding:
            return function(*args, **kwargs)
        identity = (
            SyncRun.objects.filter(pk=sync_run.pk).values_list("source_id", "job_type").first()
        )
        if identity is None or identity[1] != "earnings.calendar_window":
            return function(*args, **kwargs)
        with calendar_run_ownership(source_id=identity[0], job_type=identity[1]):
            return function(*args, **kwargs)

    return wrapped
