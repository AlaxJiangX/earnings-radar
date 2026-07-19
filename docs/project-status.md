# Earnings Radar — Project Status

> **Purpose**: Convenience snapshot for developers and Codex agents.
> This file may lag behind reality.
> **Always verify against actual Git, GitHub, repository files and CI status.**

_Last updated: 2026-07-19_

## Current Baseline

- **Branch**: `main`
- **HEAD**: `d5eeead` — feat(indexes): add public index changes page (#16)
- **GitHub**: [AlaxJiangX/earnings-radar](https://github.com/AlaxJiangX/earnings-radar)
- **CI**: [GitHub Actions](https://github.com/AlaxJiangX/earnings-radar/actions)

## Completed Stages

| Stage | Description | Status |
|-------|-------------|--------|
| 0.1 | PRD, architecture, data model, data sources, ADRs, AGENTS.md | Complete |
| 1.1 | Django project skeleton, README, CI | Complete |
| 1.2 | PostgreSQL + Docker Compose | Complete |
| 1.3 | pytest, Ruff, mypy, GitHub Actions CI | Complete |
| 1.4 | Custom User, auth, timezone preferences | Complete |
| 2.1A | DataSource, SyncRun, controlled state transitions | Complete |
| 2.1B-1 | RawDataRecord, RawDataObservation | Complete |
| 2.1B-2 | SourceEvidence | Complete |
| 2.1B-3 | DataChange, AuditRecord | Complete |
| 2.2 | Provider contracts, HTTP transport, Fake fixtures | Complete |
| 2.3 | Company, SecurityListing, controlled write services | Complete |
| 3.1A | MarketIndex (S&P 500, Nasdaq-100, DJIA, Russell 2000) | Complete |
| 3.1B | IndexMembership, lifecycle, triggers, services, selectors | Complete |
| 3.2A | Activate due memberships | Complete |
| 3.2B-1 | Offline index fixture Provider and canonical snapshot contract | Complete |
| 3.2B-2 | Offline snapshot ingestion orchestration (merged in PR #10) | Complete |
| 3.2B-3 | RawDataParseAttempt audit trail | Complete |
| 3.3 | IndexChangeEvent, IndexChangeLeg, event classification, correlation, correction | Complete |
| 3.4 | Index changes public page (`/index-changes`, filtering, HTMX pagination) | Complete |

## Active Development

- **3.2** — Live index Provider + sync command: **blocked** by source/licensing gate (ADR-005). Index change domain (3.3) and UI (3.4) are complete using fixture-first approach.
- **3.2B-2 worktrees**: Frozen at `aff5979` and `5003b63`. All relevant code was merged into main via PR #10. Worktrees preserved for reference only.

## Next Planned Stage

- `4.1` — Earnings event domain model (EarningsEvent, EarningsDateChange, candidate/canonical identity, status lifecycle, Admin). Fixture-first per ADR-005.
- `3.2` — Live index Provider + sync command (blocked by source/licensing gate)

## Blocked Product Decisions

- Earnings calendar and index data source selection / licensing
- Email service provider selection
- Public registration strategy (alpha: closed; beta: open)

## Verification

- Full pytest suite: run locally via `docker compose run --rm web pytest` or CI.
- The exact test count is intentionally not cached here; use the current collection and latest CI run.

## Key Documents

- [AGENTS.md](/AGENTS.md)
- [Product Requirements](product-requirements.md)
- [Architecture](architecture.md)
- [Data Model](data-model.md)
- [Data Sources](data-sources.md)
- [Development Roadmap](development-roadmap.md)
- [Decisions](decisions/)
