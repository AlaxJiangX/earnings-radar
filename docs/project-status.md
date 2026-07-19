# Earnings Radar — Project Status

> **Purpose**: Convenience snapshot for developers and Codex agents.
> This file may lag behind reality.
> **Always verify against actual Git, GitHub, repository files and CI status.**

_Last updated: 2026-07-17_

## Current Baseline

- **Branch**: `main`
- **HEAD**: `7a61c2a` — feat(indexes): add index constituent snapshot contract (#8)
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
| 3.2B-3 | RawDataParseAttempt audit trail | Complete |

## Active Development

- **3.2B-2 (Offline snapshot ingestion orchestration)**: implemented on `codex/3.2b-2-index-sync-orchestration`; pending merge. It intentionally stops before live Provider, membership reconciliation and the command entry point.
- **3.2B-3 (RawDataParseAttempt audit trail)**: model, recording service, consumer regression verification completed on `codex/3.2b-3-raw-data-parse-attempt`.

## Development Tooling

- **Branch**: `codex/dev-workflow-tools` — `scripts/codex-issue` and workflow docs.
- **Status**: Local auxiliary branch; no open GitHub PR as of the last update. It is outside the 3.2B-2 implementation scope.

> **Note**: If multiple worktrees are in use on this machine, run `git worktree list` for the authoritative list. Local paths and branch names shown here are examples from the development environment.

## Important Architecture Constraints

- Django modular monolith (no microservices)
- PostgreSQL only (no Redis, no Celery in MVP)
- All datetimes stored in UTC (date-only facts use `date`)
- All writes go through controlled services; Admin is read-only
- Provider adapters never write domain tables directly
- Audit records are append-only
- CIK stored as string with leading zeros; ticker is not a permanent key
- SecurityListing uses half-open `[start, end)` intervals

## Next Planned Stage

- `3.3` — IndexChangeEvent, IndexChangeLeg, membership reconciliation
- Stage 4 — Earnings events (after index stages complete)

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
