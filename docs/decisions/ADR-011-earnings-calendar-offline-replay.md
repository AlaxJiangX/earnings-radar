# Stage 4.2C Offline Replay Contract

- ADR 编号：ADR-011
- 状态：已接受（4.2C-6 Replay Foundation Ratification）
- 日期：2026-09-24
- 决策者：产品负责人
- 影响阶段：4.2C-6 foundation、4.2C-7 orchestration；不进入 4.2D
- 评审基线：`origin/main`，commit `ab23395305063e359dbbf36e685b260604fa1ba1`

## 1. Git Baseline

- 已执行 `git fetch origin`；`origin/main` 为 `ab23395305063e359dbbf36e685b260604fa1ba1`。
- 本阶段使用基于该基线的隔离 worktree。
- 分支：`codex/4.2c-6-replay-contract`。
- 原始 dirty `main` 保持不动，仍有用户已有的 `M Dockerfile`；本阶段没有覆盖或修改该文件。
- 4.2C-5 为已完成；4.2C 仍为 IN PROGRESS；4.2D 尚未开始。

本文件冻结 persistence、identity、count、pool 和 concurrency contract。4.2C-6 已按
ratification 结果实现最小 schema foundation 与 validation service；没有实现 full offline
replay orchestration、Provider fetch、4.2D selector 或其他 domain workflow。

## 2. Existing Persistence

本阶段核对了 `earnings/models.py`、`audit/models.py`、4.2C execution/ingestion/pagination/
ownership/sync-identity service、`tests/earnings/` 以及 PRD、architecture、data-model、
data-sources、roadmap 和 ADR-001 至 ADR-010。

| 实体 / 字段 | 当前事实 | replay 角色 |
|---|---|---|
| `RawDataRecord.payload`、`content_hash`、`source_url`、`request_fingerprint`、`first_sync_run` | 保存原始 bytes 与来源身份；有大小、hash、凭据和 URL 安全校验 | **FACT / REPLAYABLE INPUT / IMMUTABLE HISTORY**；唯一主要输入 |
| `RawDataObservation.sync_run`、`raw_data_record`、`observed_at` | 把一个 raw record 绑定到一次 SyncRun；`(sync_run, raw_data_record)` 唯一 | **FACT / IMMUTABLE HISTORY**；重建 source run 的 evidence 集合，replay run 需要新增自己的 observation |
| `RawDataParseAttempt.observation`、`parser_version`、`status` | `(observation, parser_version)` 唯一；同状态重复写复用，状态冲突拒绝 | **FACT / IMMUTABLE HISTORY**；parse replay 的结果记录 |
| `EarningsCalendarObservation` | 以 `(raw_data_record, parser_version, provider_event_id)` 唯一；追加式保存 normalized facts | **DERIVED / REGENERATABLE**；可验证 normalized replay 幂等，不能当 replay 输入 |
| `SyncRun.scope`、`job_type`、`source`、`idempotency_key`、`status`、时间戳和现有 counts | 保存窗口、provider、pool as-of/hash、selector version、terminal 状态和当前 fetch/写入计数；4.2C-6 新增受约束 replay metadata | **FACT / REPLAY METADATA**；foundation 已提供 lineage、run mode、contract version、digest 与 replay count |
| `fetched_count` | 当前表达 provider/page fetch 进度 | **FACT**；offline replay 必须保持为零，不能改写成 replay 条数 |
| `persisted_count`、`page_count` | 当前 `SyncRun` 没有这两个一等字段 | **NOT NEEDED**；V1 replay unit 是整个 terminal run，不能用未持久化 page subset 冒充输入 |
| monitoring pool contract | source run 的 canonical scope 已保存 as-of、hash 和 selector version；4.2C-6 只核对请求值与这些持久化值一致 | **IMMUTABLE REPLAY CONTRACT**；不调用 4.2D selector，也不重算当前的 monitoring pool |
| request/page identity | `RawDataRecord` 有 request fingerprint；`RawDataObservation` 没有 page index/terminal manifest | **FACT（部分）**；首版只允许整个 terminal source run，不支持隐式 page subset 或补页 |

当前 persistence 已补齐 replay source lineage、contract version、input digest、独立 replay
计数、受约束 run mode 和 replay-compatible stale recovery。尚无完整 replay orchestration、
company matching 或 4.2D monitoring-pool selector。

## 3. Offline Replay Definition

正式定义：

> **Offline replay 是在不重新访问 Provider 的情况下，仅使用已经持久化的历史 raw evidence，重新执行解析与规范化流程，并生成新的、可审计的 replay 结果。**

强制不变量：

```text
Provider network fetch = 0
```

Replay 的唯一主要 source of truth 是 source run 关联的 `RawDataObservation` 集合所指向的
`RawDataRecord.payload`。旧的 normalized observation 只能作为幂等校验结果，不能作为重新解析
的输入。Replay 不在 4.2C 写 `EarningsReconciliationDecision`、candidate 或 `EarningsEvent`。

## 4. Retry vs Offline Replay

| Contract | Retry | Offline Replay |
|---|---|---|
| Provider fetch | 从 logical window 起点重新 fetch；允许网络 | **必须为 0**；仅读取 DB 中已保存 payload |
| 新 SyncRun | 是；新的 request ID 和 idempotency key | 是；独立 replay identity，不能复用 source/retry run |
| `source` | 原 earnings calendar `DataSource` | 原 earnings calendar `DataSource` |
| `job_type` | `earnings.calendar_window` | `earnings.calendar_window`，但 `window_kind` 必须是 `replay` |
| logical window | 重试原窗口，重新走 provider pagination | 复用 source run 的 canonical window；初版输入为整个 terminal source run |
| raw evidence 来源 | 新网络响应，可新建或复用 RawDataRecord | 现有 `RawDataRecord.payload`，不得伪造新的 fetch evidence |
| old run 是否改变 | 否；原 FAILED/PARTIAL 保留 | 否；source run 的 status、counts、heartbeat、finished_at 均不变 |
| monitoring pool hash | 重试前核对原 pool hash | **Option A**：只核对 replay request 与 source run 已持久化的 as-of/hash/version；不在 4.2C 重算 selector |
| lineage | retry SyncRun → 新 RawDataObservation/Record → retry ParseAttempt | replay SyncRun → replay RawDataObservation → 既有 RawDataRecord → source RawDataObservation → source SyncRun |
| parse attempt | 对新/复用 observation 按 parser version 写入 | 对 replay observation + parser version 写入；同 replay 重复时复用 |
| normalized result | 新 raw identity 允许产生新的 normalized observation | 同 raw + parser + provider event ID 复用；parser version 改变时新增 append-only revision |
| idempotency | 失败/partial 重试使用新 key；同 key 的 terminal run 可复用 | `run mode + source + job type + source run + logical window + parser/contract + input digest` 形成 deterministic identity；同 identity 复用 |

表中所有 replay 必需项均已由 4.2C-6 persistence/service foundation 覆盖；未持久化
page subset 与 `persisted_count` 明确不属于 V1 contract。

## 5. Replay Source of Truth

| 事实 | 分类 | 结论 |
|---|---|---|
| `RawDataRecord.payload`、hash、URL、request fingerprint | FACT / IMMUTABLE HISTORY | **Replay 输入**；必须按主键重新加载并校验 bytes 与 hash |
| source run 下的 `RawDataObservation` 集合 | FACT / IMMUTABLE HISTORY | **Replay 输入选择器**；默认全部纳入，按稳定序列形成 input digest |
| `RawDataParseAttempt` | FACT / DERIVED HISTORY | replay 输出/审计，不作为输入；source attempt 不覆盖 |
| `EarningsCalendarObservation` | DERIVED / REGENERATABLE | 只用于验证 normalized 幂等或保存新 parser revision |
| `SyncRun.scope` 与 terminal metadata | FACT / REPLAY METADATA | 决定 window、provider、as-of、pool hash 和 source status；必须重新加载 |
| 当前 provider response、今日 pool、旧 normalized 文本 | 非法输入 | 不允许参与 offline replay |

唯一主要 source of truth：**source SyncRun 关联的已持久化 raw evidence（RawDataObservation →
RawDataRecord.payload）**。

## 6. Replay Unit

本阶段选择一个粒度：

> **Replay unit = 一个 terminal source `SyncRun` 的完整、已持久化 `RawDataObservation` 集合。**

规则：

- `RUNNING` source run 不可 replay；
- 初版不支持自由 page subset、按 ticker 过滤、补缺页或以今天的 pool 补齐；
- input 集合按稳定的 raw identity tuple（`request_fingerprint`、`content_hash`、
  `source_url`、`payload_size_bytes`）排序；RawDataRecord UUID 是数据库 row identity，
  不作为 digest 输入。摘要每条 observation 的 request fingerprint、content hash、
  payload size 和受控 response metadata；
- source `SUCCEEDED` 必须能证明 fetch/page count 与 raw observation 集合一致；不一致时 fail closed；
- source `PARTIAL` / `FAILED` 只能 replay 已保存的 subset，并保留“窗口不完整”语义。

## 7. Replay Identity

实现不得复用 scheduled/manual/backfill/retry builder；需同时使用显式
`run_mode = "replay"` 与 `window_kind = "replay"`。canonical identity：

```text
SHA256(canonical_json({
  run_mode,
  source_id,
  job_type,
  source_sync_run_id,
  logical_window,
  parser_version,
  replay_contract_version,
  replay_input_digest
}))
```

同一 source run、logical window、parser context、contract version 和 input digest 必须得到
同一个 replay identity；任一组成值变化必须产生新的 replay identity。数据库使用 replay-only
partial unique constraint 作为最终防线。V1 不引入独立的 replay request ID：需要新 replay
identity 时必须改变有语义的 parser/contract context，而不是用随机请求号复制同一计算。

## 8. Lineage Contract

必须能查询以下链路：

```text
replay SyncRun
  -> replay RawDataObservation
  -> existing immutable RawDataRecord
  -> source RawDataObservation
  -> source SyncRun
```

Foundation 采用一等 `replay_source_sync_run`（nullable `PROTECT` FK）和
`replay_input_digest`。服务会重新加载 source run，并验证 ingestion mode、同 source、同
job type、terminal status、canonical scope 和 raw observation count；replay scope 必须与
source scope 完全相同，只允许把 `window_kind` 改为 `replay`。DB 约束禁止自引用，并通过
replay-only unique constraint 固化身份。

Replay 不得复制/修改 RawDataRecord，不得覆盖 source run 的任何状态或计数；所有 parse、
normalized 和失败记录必须指向 replay observation 或其 raw record，并保持 append-only。

## 9. Raw Evidence Contract

- 先按主键重新加载 source run、RawDataObservation 和 RawDataRecord；不信任调用方内存对象。
- 逐条验证 source、request fingerprint、sanitized URL、payload bytes、payload size 和
  `sha256(payload) == content_hash`。
- raw record 缺失、payload/hash 不一致、source 关系错误或 evidence 不在 source run 下时，
  在任何 replay 写入前 fail closed。
- Replay 使用已有 raw record；不得调用 Provider、不得伪造新的 network fetch、不得删除或
  修正旧 raw history。
- source raw evidence 为空时，只有当 source 是合法空日历且其 terminal 结果可验证，才允许
  replay 成功；否则为 `FAILED`，不创建伪成功结果。

## 10. ParseAttempt / Parser Version Contract

- 每个 replay observation + parser version 最多一个 `RawDataParseAttempt`。
- 同一 replay run 重复处理相同 observation/parser/status 时复用现有 attempt；terminal
  status 不同则 fail closed。
- parser failure 必须保留 replay raw lineage，并写安全的 data/system/unsupported error
  summary；不得创建 normalized observation。
- parser version 是 identity 的组成部分；改变 parser version 必须是新的 replay identity，
  不能覆盖旧 attempt 或旧 normalized 结果。

## 11. Normalized Persistence Contract

- 使用现有 `record_earnings_calendar_observation` 的 immutable-field 校验与唯一键。
- 同一 raw record + parser version + provider event ID 的 normalized row 已存在且字段一致时，
  replay 返回 reused/no-op；字段不一致时 fail closed。
- 新 parser version 可创建新的 append-only normalized observation revision；旧版本仍保留。
- 4.2C replay 不创建/修改 `EarningsReconciliationDecision`、candidate 或 `EarningsEvent`。

## 12. Completion / Partial Semantics

- source `SUCCEEDED` 且所有选中 evidence 完成 parse/normalize、无 data/system error：replay
  `SUCCEEDED`；合法空日历可成功。
- 任一 evidence 出现 parse/data/system/unsupported failure：按现有 C-3 fail-closed 语义为
  `PARTIAL` 或 `FAILED`，保留已写 replay lineage。
- source `PARTIAL` / `FAILED` 的 replay **最多为 `PARTIAL`**；它不能宣称原 logical window
  完整。若 replay 自身出现致命错误，可为 `FAILED`。
- replay 的 network fetch count 必须为 0；不可把 replay 条数写入 `fetched_count`。
- old source run 的 status、counts、timestamps 和 history 不变。

## 13. Preconditions

实现前必须满足：

1. source run 存在且 terminal；job type/source/provider/scope 通过 canonical validator；
2. source scope 的 window、pool as-of/hash、selector version 完整且无凭据；
3. replay request 的 monitoring pool as-of/hash/selector version 与 source run 持久化
   immutable scope 完全一致；Option A 不重算 selector；
4. 所有 source raw observations 与 records 完整、hash 一致、来源链可解析；
5. replay identity 输入已规范化，run mode、parser version 和 contract version 非空；
6. replay persistence foundation（第 18 节）已完成；
7. provider/network adapter 在 replay path 不可被调用。

## 14. Concurrency Contract

4.2C 选择 correctness 优先：offline replay 与 scheduled/retry/manual ingestion 共用
`(source, job_type)` advisory lock。

Foundation 的 replay start 在该锁域内完成 source/contract 校验和 run creation；fresh
`RUNNING` replay 会让后续 scheduled/retry start 被现有 stale gate 判为 busy，不新增 replay
专属锁。

| 场景 | 结果 |
|---|---|
| scheduled ingestion `RUNNING` → replay starts | **REJECT / fail fast**；已有 owner 释放后由调用方按 identity 重试 |
| replay `RUNNING` → scheduled ingestion starts | **REJECT / fail fast**；scheduled 不绕过 replay owner |
| 同一 replay identity 并发启动 | DB unique + owner lock；一个创建，另一个复用/报告 busy |
| 不同 source 或不同 job type | 不共享该锁域，按各自 contract 执行 |

Replay 不允许通过更细的锁域换取吞吐；normalized duplicate 只能作为最后的 DB/Service 幂等
防线，不能替代运行所有权。

## 15. Idempotency Contract

同一 original/source run、同一 replay parameters、同一 parser/version context 和同一 input
 digest 重复触发两次：

- terminal replay run：返回已有 run；不新增 replay observation、parse attempt 或 normalized row；
- existing `RUNNING` replay：不启动第二个 worker，fail fast；
- context/digest 不一致：fail closed，不复用错误的 run；
- parser version 或 replay contract version 改变：显式创建新的 replay identity；历史保留。

## 16. Crash / Stale Recovery

- replay 中途崩溃时，run 不得伪造 success；保留已经写入的 raw observation、parse attempt 和
  normalized 结果。
- `RUNNING` replay 沿用 `EARNINGS_CALENDAR_STALE_AFTER_SECONDS` 的 heartbeat 所有权检查；
  stale run 只能在持有 `(source, job_type)` lock 时被标为 `PARTIAL`（已有 evidence）或
  `FAILED`（没有 evidence），不得直接复活旧 run。
- stale run 的既有 ParseAttempt 和 normalized rows 不回滚、不删除、不改写。
- `replayed_count` 只从该 replay run 下已持久化的 RawDataObservation 重建；计数领先于事实
  时 fail closed，不从内存累加值猜测。
- stale terminal replay 不自动复活。V1 的同一 identity 继续复用 terminal result；只有在
  parser 或 replay contract context 变化时才形成新的 replay identity。后续若需要同上下文的
  operational replay retry，必须另行批准 request identity，不能偷偷复用 ingestion retry key。

## 17. Missing Evidence Behavior

| 情况 | 预期行为 |
|---|---|
| original/source run 不存在 | 立即拒绝；不创建 replay run，不写任何 lineage |
| original/source run `RUNNING` | 立即拒绝；等待 source terminal 后再 replay |
| source raw observation 缺失 | fail closed；source run 不变；若 replay 已部分写入则保留并终止为 PARTIAL/FAILED |
| source fetch count 与 persisted raw observations 不一致 | fail closed；不猜测缺失 page，也不把未知 raw 补成证据 |
| RawDataRecord 缺失或 hash/bytes 不一致 | fail closed；不信任旧 normalized result，不伪造 raw |
| source partial evidence | 只处理现有 evidence，replay 最多 PARTIAL，不能补 fetch |
| 合法空 source | 仅在 source terminal 语义和计数可验证时成功 |
| parser failure | 保留 raw/parse failure lineage，不创建 normalized row |
| pool hash mismatch | 在 replay writes 前拒绝；不使用当前 pool，不修改 source run |

## 18. Ratified Foundation

**Schema change required = YES；4.2C-6 foundation 已实现。**

最终采用 Option A，不实现 4.2D selector。最小 `SyncRun` 扩展如下：

| 字段 / constraint | Purpose | Nullability / default | Historical rows | Constraint / index |
|---|---|---|---|---|
| `run_mode` | 区分 ingestion 与 replay，避免只靠自由 JSON | 非空，`ingestion` | migration 后全部为 ingestion | choices + DB check |
| `replay_source_sync_run` | replay → source SyncRun 的一等 lineage | nullable；replay 必须非空 | 历史 ingestion 为 NULL | `PROTECT`；replay-only unique identity；self-reference check |
| `replay_contract_version` | 固定 replay 语义版本 | 非空字符串，默认空串 | 历史 rows 为空串，表示不适用 | replay 必须含非空白字符 |
| `replay_input_digest` | 证明一次 replay 消费的完整 raw manifest | 非空字符串，默认空串 | 历史 rows 为空串，表示不适用 | replay 必须为 64 位小写 SHA-256 hex |
| `replayed_count` | 从 replay-linked RawDataObservation 重建的独立进度 | 非负整数，默认 0 | 历史 rows 为 0，表示无 replay progress | non-negative check；不与 `fetched_count` 混用 |
| `parser_version` | 固定 parse context；现有字段 | 非空字符串 | ingestion 语义不变 | replay-only check 要求非空 |

Replay identity 另有 partial unique constraint，覆盖 source、job type、source SyncRun、parser
version、contract version 和 input digest。即使调用方伪造不同 `idempotency_key`，数据库仍会
拒绝第二条 equivalent replay run。

`window_kind` enum 增加 `replay`，但 manual/backfill/retry request-key builder 不接受它。
`EarningsCalendarWindowKind.REPLAY` 只与 `run_mode=replay` 一起由 foundation service 使用。

ParseAttempt 与 normalized revision 不新增 schema：

- `RawDataParseAttempt` 的 `(observation, parser_version)` 唯一键已支持同一 raw record 的
  append-only parser revisions；
- `EarningsCalendarObservation` 的 `(raw_data_record, parser_version, provider_event_id)`
  唯一键已支持 parser-version revision，旧 row 保持不变。

Migration `audit/0009_syncrun_offline_replay_foundation.py` 为历史 SyncRun 保留原 status、
scope、counts、timestamps 和 history；仅填充不适用默认值，不回填或猜测 replay lineage。
Reverse migration 删除新增 fields 与 constraints，不删除历史 SyncRun。Migration test 覆盖
forward、reverse、再次 forward 和 historical row preservation。

## 19. Required Tests

以下测试应在 foundation migration 后进入 replay implementation；标有“foundation” 的项目
不能提前用自由 JSON 或伪造计数绕过：

| Test | Expected Behavior |
|---|---|
| replay success | terminal source 的全部 evidence 无错误时新建 replay run 并成功，`network_fetch_count=0` |
| no Provider fetch | provider/page source 未被调用；所有输入来自已有 RawDataRecord |
| original run missing | fail closed；不创建 replay run/lineage |
| original run RUNNING | fail closed；不复活 source |
| raw evidence missing | fail closed；source unchanged；已有部分 replay lineage 保留 |
| partial evidence | 只处理已保存 subset，replay 最多 PARTIAL，不补 fetch |
| same replay repeated | deterministic identity 复用 terminal run，不新增 observation/attempt/normalized row |
| parser failure | raw lineage 保留，parse attempt 为 data/system/unsupported，normalized 不创建 |
| normalized duplicate | immutable fields 一致时 reused；冲突时 fail closed |
| replay crash | 不伪造 success；已写历史保留 |
| stale replay recovery | 使用相同 stale threshold/owner lock，终止旧 run并用新 key 重放，不删除旧历史 |
| scheduled + replay concurrency | 共用 `(source, job_type)` lock；任一持有 owner 时另一方 fail fast |
| pool hash mismatch | replay writes 前拒绝；不使用当前 pool |
| lineage preserved | replay observation/attempt 可追溯到 source run 与同一 RawDataRecord |
| original run unchanged | source status/counts/heartbeat/finished_at/history 完全不变 |

4.2C-6 foundation 已覆盖 lineage、input digest、独立计数、pool contract validation、
parallel-lock rejection、stale recovery 和 append-only revision primitives。完整 replay
success/failure orchestration、page manifest consumption 和 domain no-op 仍属于 4.2C-7，
不能因为 foundation 测试通过而宣称完整 replay 已实现。

## 20. Explicit Non-Goals

4.2C-6 foundation 明确不做：

- full replay orchestration loop、parse-all-source-run executor 或 normalized replay executor；
- replay source adapter、management command、API 或 UI；
- Provider fetch/network access；
- monitoring pool selector、company matching、candidate、reconciliation 或 EarningsEvent write；
- historical backfill、live provider refetch 或 Stage 4.2D/4.2E；
- Celery、Redis、queue infrastructure；
- 任何 domain write、通知或 destructive merge。

## 21. Next Implementation Scope

在 4.2C-6 foundation gate 通过后，下一单一实现阶段应是：

> **Stage 4.2C-7 — Offline Replay Orchestration Implementation**

4.2C-7 应实现：

- 基于 4.2C-6 digest、lineage、count 和 lock primitive 的 DB-backed replay executor；
- 完整 source run raw manifest 的 replay observation、parse attempt 和 normalized persistence；
- crash/stale recovery 与 terminal completion；
- provider-call prohibition regression。

4.2C-7 仍不实现 4.2D company matching、candidate generation 或 live selector。

## 22. Ratification Decision

```text
Monitoring pool strategy:
Option A

Why:
offline replay 的目标是基于已持久化 raw evidence 重做 parse / normalized pipeline；source
run 已保存 immutable monitoring pool as-of/hash/selector version。4.2C foundation 只验证 replay
request 与这些 persisted values 完全一致，不重算历史 selector，也不使用 today's pool。

Does this enter Stage 4.2D:
NO
```

Option A 的边界是：本阶段可以证明 replay 使用了 source run 当时持久化的 pool contract，
不能独立证明当年的 selector calculation 本身正确。后者仍由 Stage 4.2D 的历史 selector
实现与后续审计负责。

4.2C-6 gate decision：

```text
PASS — replay foundation is ready for offline replay orchestration
```

4.2C 整体继续保持 IN PROGRESS；4.2D 保持 NOT STARTED。
