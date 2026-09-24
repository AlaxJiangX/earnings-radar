# Stage 4.2C Offline Replay Contract / Planning Gate

- ADR 编号：ADR-011
- 状态：提案（Planning Gate；未接受）
- 日期：2026-09-24
- 决策者：待产品负责人确认
- 影响阶段：4.2C-6；不进入 4.2D
- 评审基线：`origin/main`，commit `ab23395305063e359dbbf36e685b260604fa1ba1`

## 1. Git Baseline

- 已执行 `git fetch origin`；`origin/main` 为 `ab23395305063e359dbbf36e685b260604fa1ba1`。
- 本阶段使用基于该基线的隔离 worktree。
- 分支：`codex/4.2c-6-replay-contract`。
- 原始 dirty `main` 保持不动，仍有用户已有的 `M Dockerfile`；本阶段没有覆盖或修改该文件。
- 4.2C-5 为已完成；4.2C 仍为 IN PROGRESS；4.2D 尚未开始。

本文件只做 persistence audit、正式 Contract 和 Planning Gate。没有实现 replay、没有写
migration、没有新增依赖，也没有初始化 Django 或进入 4.2D。

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
| `SyncRun.scope`、`job_type`、`source`、`idempotency_key`、`status`、时间戳和现有 counts | 保存窗口、provider、pool as-of/hash、selector version、terminal 状态和当前 fetch/写入计数 | **FACT / REPLAY METADATA**；需要新增明确 replay lineage 与 replay 计数语义 |
| `fetched_count` | 当前表达 provider/page fetch 进度 | **FACT**；offline replay 必须保持为零，不能改写成 replay 条数 |
| `persisted_count`、`page_count` | 当前 `SyncRun` 没有这两个一等字段 | **UNRESOLVED CONTRACT**；不能假设它们存在 |
| monitoring pool selector | ADR-010 要求按 as-of + selector version 重算并核对 hash；当前 selector 尚未实现 | **REGENERATABLE PRECONDITION**；缺失时必须 BLOCKED |
| request/page identity | `RawDataRecord` 有 request fingerprint；`RawDataObservation` 没有 page index/terminal manifest | **FACT（部分）**；首版只允许整个 terminal source run，不支持隐式 page subset 或补页 |

当前 persistence 能支撑 raw、parse 和 normalized 的幂等基元，但没有完整的 replay
orchestration、replay source lineage、独立 replay 计数和 monitoring-pool selector。

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
| monitoring pool hash | 重试前核对原 pool hash | 重新按原 `monitoring_pool_as_of` + `selector_version` 计算并核对原 hash；selector 缺失或不一致则拒绝 |
| lineage | retry SyncRun → 新 RawDataObservation/Record → retry ParseAttempt | replay SyncRun → replay RawDataObservation → 既有 RawDataRecord → source RawDataObservation → source SyncRun |
| parse attempt | 对新/复用 observation 按 parser version 写入 | 对 replay observation + parser version 写入；同 replay 重复时复用 |
| normalized result | 新 raw identity 允许产生新的 normalized observation | 同 raw + parser + provider event ID 复用；parser version 改变时新增 append-only revision |
| idempotency | 失败/partial 重试使用新 key；同 key 的 terminal run 可复用 | `source run + request + parser + contract + input digest` 形成 deterministic identity；同 identity 复用 |

表中无法从当前 persistence 直接推出的关键项已标记为 `UNRESOLVED CONTRACT`，并在第 18 节
列为 schema/foundation gap。

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
- input 集合按 `RawDataObservation.observed_at ASC, RawDataObservation.id ASC` 排序，摘要每条
  observation 的 ID、raw record ID、content hash 和 request fingerprint；
- source `SUCCEEDED` 必须能证明 fetch/page count 与 raw observation 集合一致；不一致时 fail closed；
- source `PARTIAL` / `FAILED` 只能 replay 已保存的 subset，并保留“窗口不完整”语义。

## 7. Replay Identity

实现不得复用 scheduled/manual/backfill/retry builder；需增加显式 `window_kind = "replay"`。
建议的 canonical identity：

```text
SHA256(canonical_json({
  source_key,
  job_type,
  source_sync_run_id,
  replay_request_id,
  parser_version,
  replay_contract_version,
  source_scope_digest,
  ordered_input_digest,
}))
```

同一 `source_sync_run_id + replay_request_id + parser/version context + input digest` 必须复用
已有 replay run；任一组成值变化必须产生新的 replay identity。Replay request ID 必须显式、
可审计且不含凭据。

## 8. Lineage Contract

必须能查询以下链路：

```text
replay SyncRun
  -> replay RawDataObservation
  -> existing immutable RawDataRecord
  -> source RawDataObservation
  -> source SyncRun
```

推荐将 `replayed_from_sync_run`（nullable `PROTECT` FK）和 `replay_input_digest` 作为一等
metadata。若产品选择只存 scope，必须先提供受约束的 resolver、字段校验和 digest 校验，
不能依赖自由 JSON 文本。

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
3. selector 能按原 as-of + version 重算 monitoring pool 并得到相同 hash；
4. 所有 source raw observations 与 records 完整、hash 一致、来源链可解析；
5. replay identity 输入已规范化，request ID/parser/contract version 非空；
6. replay persistence foundation（第 18 节）已完成；
7. provider/network adapter 在 replay path 不可被调用。

## 14. Concurrency Contract

4.2C 选择 correctness 优先：offline replay 与 scheduled/retry/manual ingestion 共用
`(source, job_type)` advisory lock。

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
- 重新回放使用新的 `replay_request_id` / idempotency key，仍然不访问 Provider；现有 normalized
  unique key 保证不会产生重复 derived result。
- 当前 stale helper 只理解 `fetched_count` 与 raw observation 数量，无法正确表达 replay
  progress；必须在 persistence foundation 中增加独立 replay progress 语义后才能实现。

## 17. Missing Evidence Behavior

| 情况 | 预期行为 |
|---|---|
| original/source run 不存在 | 立即拒绝；不创建 replay run，不写任何 lineage |
| original/source run `RUNNING` | 立即拒绝；等待 source terminal 后再 replay |
| source raw observation 缺失 | fail closed；source run 不变；若 replay 已部分写入则保留并终止为 PARTIAL/FAILED |
| RawDataRecord 缺失或 hash/bytes 不一致 | fail closed；不信任旧 normalized result，不伪造 raw |
| source partial evidence | 只处理现有 evidence，replay 最多 PARTIAL，不能补 fetch |
| 合法空 source | 仅在 source terminal 语义和计数可验证时成功 |
| parser failure | 保留 raw/parse failure lineage，不创建 normalized row |
| pool hash mismatch | 在 replay writes 前拒绝；不使用当前 pool，不修改 source run |

## 18. Schema Gap

**Schema change required = YES。**

当前 schema 缺少以下可安全实现 replay 所需的语义：

1. `SyncRun` 没有 `window_kind = replay` 的受约束 identity 入口，也没有一等
   `replayed_from_sync_run` 关系或等价受约束 lineage metadata；source run ID 放在自由 scope 中
   不足以保证可查询、可校验和可审计；
2. 没有独立 `replayed_count`（及必要的 replay progress/error 计数）。复用 `fetched_count`
   会违反“零 provider fetch”语义，并且现有 stale helper 可能把 replay 进度误判为 raw fetch
   进度；
3. 没有 replay input digest / contract version 的持久化位置，无法证明两次 replay 读取了同一
   evidence 集合和规则上下文。

最小 foundation / migration stage（本轮只列出，不执行）：

- 为 `SyncRun` 增加可为空、`PROTECT` 的 replay source relation，或先由产品确认并实现严格
  的 scope resolver；
- 增加 `replay_contract_version`、`replay_input_digest` 和独立 `replayed_count`（必要时再
  增加 replayed failure counters），现有运行默认保持空/零，不回写历史；
- 为 `window_kind = replay`、replay identity builder、stale progress 和 counts 增加模型/服务
  约束与迁移；
- 提供可按 as-of/version 重算 pool hash 的 selector service。它是代码 foundation，不应被
  伪装成 4.2C replay 的顺手实现。

迁移风险：需要为已存在的 SyncRun 提供无破坏性默认值，保持现有唯一约束和 append-only 历史；
必须验证旧 scheduled/retry run 不会被误识别为 replay，且升级期间 stale recovery、counts 和
source lineage 读取兼容。不得删除或重写既有 raw/parse/normalized/history。

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

必须等 schema foundation 后才能可靠实现：replay success 的独立计数、stale replay recovery、
lineage query、input digest idempotency、source/replay 并发回归。Raw bytes/hash 校验、parser
failure 和 normalized duplicate 的底层单元测试可以先用现有 primitives 覆盖，但不能宣称完整
replay 已实现。

## 20. Explicit Non-Goals

本阶段明确不做：

- replay source adapter、Replay service、management command、API 或 UI；
- migration 或 `SyncRun`/`RawDataObservation`/`RawDataParseAttempt`/earnings schema 修改；
- monitoring pool selector 的实现（只列为 precondition/foundation）；
- historical backfill、live provider refetch、company matching、candidate generation、
  reconciliation、Stage 4.2D/4.2E；
- Celery、Redis、queue infrastructure；
- 任何 domain write、通知或 destructive merge。

## 21. Next Implementation Scope

在产品接受本提案并完成第 18 节 foundation 后，下一单一实现阶段应是：

> **Stage 4.2C-6 — Offline Replay Orchestration Implementation**

可能修改的文件（仅用于下一阶段规划）：

- `audit/models.py` 与对应 audit migration；
- `audit/services/sync_runs.py` 及 stale/progress helper；
- `earnings/services/calendar_sync_identity.py`；
- 新的 DB-backed replay source/orchestration service；
- monitoring-pool selector 所属 service（若已被提前抽取）；
- `tests/earnings/` 与 audit replay contract tests。

本轮不修改这些文件。

## 22. Planning Gate Decision

**BLOCKED — schema/foundation gap must be resolved first.**

Blocking evidence：

1. 当前 `SyncRun` 无 replay source lineage、`replayed_count` 或 input digest 的受约束持久化
   语义；
2. 当前 window identity 只接受 scheduled/manual/backfill/retry，不能把 replay 与 retry 明确
   区分；
3. monitoring-pool selector 尚未实现，不能按原始 as-of 证明 pool hash；
4. 当前 stale/count helper 只按 fetch 语义工作，无法在零网络 replay 中安全表示进度。

Minimum foundation / migration stage：先接受并实现第 18 节的最小 schema 与 selector foundation，
再开启第 21 节的 orchestration implementation。直到那时，4.2C 继续保持 IN PROGRESS，4.2D
保持 NOT STARTED。
