# ADR-010：财报日历 Observation、External Identity 与 Reconciliation 契约

- 状态：已接受
- 日期：2026-09-20
- 决策者：产品负责人
- 影响阶段：4.2A、4.2B、4.2C、4.2D、4.2E、4.2F

## 背景

Stage 4.1 已完成 EarningsEvent canonical/candidate identity、日期变化历史、status lifecycle
和 candidate promotion。Stage 4.2 需要在此基础上接入第三方财报日历 Provider、同步编排和
跨 Provider reconciliation，但以下问题在 Stage 4.2 Planning 结束时仍未冻结：

- provider external identity 存放在哪里，缺失时如何处理；
- 跨 Provider 是否允许 fuzzy 自动合并；
- 第三方 earnings calendar 可以自动写哪些字段；
- 人工 decision 是否会被后续自动同步覆盖；
- 空日历、partial pagination 是否代表删除或取消；
- duplicate/canonical collision 是否允许 destructive merge；
- monitoring pool、SyncRun scope 与 replay identity 如何定义；
- 何时允许接入真实 Provider。

本 ADR 只冻结 Stage 4.2 的 contract，不实现 model、migration、Provider、parser、sync service、
reconciliation service 或 management command。本文中的 MUST / SHALL / MUST NOT 是仓库契约；
"4.2B planned" 表示方向已批准但数据库与代码尚未存在。任何实现不得把 planned 内容描述为
已实现事实。

## 决策

### 1. 决策索引

| 决策 | 结论 |
|---|---|
| 4.2A-01 Stable Provider Event Identity | `provider_event_id` 是 normalized observation / reconciliation 层的必要条件；source identity 与 canonical identity 分离 |
| 4.2A-02 Cross-provider Automatic Match Threshold | V1 只采用 exact-only automatic reconciliation，不实现 fuzzy auto-merge threshold |
| 4.2A-03 Source Precedence / Manual Override | 第三方 calendar 只能自动写 `estimated_release` / `release_session`；append-only manual decision 优先 |
| 4.2A-04 Sync Window / Backfill / Empty Calendar | forward 90 天、past 30 天必须配置化；backfill 显式触发；合法空日历是成功响应且不触发 mutation |
| 4.2A-05 Duplicate / Merge Representation | No destructive merge；loser 保留，通过 append-only decision/mapping 指向 winner canonical |
| 4.2A-06 Provider / License Gate | 4.2A-4.2E fixture-first；4.2F 在人工 license checklist 完成前保持 BLOCKED |

### 2. 分层与 external identity（4.2A-01）

财报日历数据 MUST 按以下方向流动：

```text
raw bytes
  -> parser
  -> EarningsCalendarObservation
  -> EarningsReconciliationDecision
  -> EarningsEvent
```

- `provider_key + provider_event_id` ONLY 表示 source identity，MUST NOT 进入
  `EarningsEvent.identity_key`，也不得成为 canonical identity 的一部分。
- `provider_event_id` MUST 是非空、稳定、由上游提供的字符串。
- MUST NOT 使用 `company + fiscal label + provider date`、ticker、release date 或其他字段
  合成 provider event ID。
- 同一 provider event ID 下的事实变化表示 correction / new observation，MUST NOT 自动表示
  新的 canonical earnings event。
- external ID 已映射到不同 company 或不同 fiscal period 时 MUST fail closed，写入
  review/collision decision，不得自动改写 mapping。
- 一个 provider event 当前最多映射一个 EarningsEvent；多个 provider events MAY 映射到同一个
  EarningsEvent。

上游单条记录缺失 stable event ID 时：

- raw bytes MUST 继续保存；
- RawDataObservation 与 RawDataParseAttempt MUST 继续保留；
- MUST NOT 创建 `EarningsCalendarObservation`；
- MUST NOT 创建 EarningsEvent candidate；
- MUST NOT 进入自动 reconciliation；
- parse / ingestion MUST 以明确的 unsupported-identity 或 data-quality reason 留痕；
- MUST NOT 为了继续流程而人工合成 provider_event_id。

如果一个候选 live provider 本身无法提供可依赖的 stable event identity，它默认不符合 4.2F
live provider contract。除非未来另开 ADR 明确批准新的 identity strategy，否则 MUST NOT
接入生产。

### 3. Exact-only automatic reconciliation（4.2A-02）

V1 自动 match / dedup ONLY 允许以下能够确定同一 canonical identity 的情况：

1. 同 source + 同 external ID；
2. 同 source + 不同 external ID，但 company 已唯一确定、`period_end_date` 相同、
   normalized `period_type` 相同；
3. 不同 provider，但满足以下全部条件：
   - CIK 唯一一致，或唯一 `exchange + ticker` as-of match；
   - `period_end_date` 相同；
   - normalized `period_type` 相同；
   - 52/53-week metadata 在双方都存在时不得冲突。

MUST NOT 自动 match：

- `period_end_date` 缺失；
- 只有 fiscal label；
- 仅 release date 接近；
- ticker-only 且 listing 不唯一；
- 跨交易所歧义；
- fiscal label 冲突；
- 52/53-week 信息冲突；
- external ID 已指向不同 company / period；
- 任何需要相似度阈值才能判断的场景。

release date proximity、label 相似度、名称相似度等 MAY 作为 `match_factors` 或人工 review
evidence，MUST NOT 成为 identity。Stage 4.2E MUST NOT 实现 fuzzy auto-merge threshold。

### 4. 字段 authority、precedence 与 manual override（4.2A-03）

Stage 4.2 third-party earnings calendar 自动写 domain 的字段 ONLY 包括：

- `estimated_release`
- `release_session`

两者 MUST 通过现有 schedule domain service 写入。Stage 4.2 third-party data MUST NOT 自动写：

- `confirmed_release`
- `earnings_release`
- `conference_call`
- `scheduled_confirmed` / `released` / `cancelled` status
- 任何属于 4.4 / 4.5 authority 的字段。

provider absence MUST NOT 成为 cancellation、deletion 或任何 status mutation 的证据。

Stage 4.2 MUST NOT 新增可变的 `locked` 字段。append-only manual reconciliation decision 的
authority 高于 4.2 third-party calendar：

- 某字段存在有效的、最新的 resolved manual decision 时，后续 4.2 自动同步 MUST NOT 用第三方
  calendar 值覆盖该人工结果；
- 只有新的人工 decision 明确 supersede，或未来 4.4 / 4.5 的更高 authority source contract
  明确允许替代，才可变更；
- authority MUST 从 append-only decision history 推导，而不是在 EarningsEvent 上增加 lock
  flag。

### 5. Sync window、backfill 与 empty calendar（4.2A-04）

默认配置：

```text
forward_horizon_days = 90
past_correction_days = 30
```

两个值 MUST 是 config，MUST NOT 硬编码到业务逻辑。正常 daily logical window 为
`today - past_correction_days` 到 `today + forward_horizon_days`。

历史 backfill：

- ONLY 通过显式 manual / operator 请求触发；
- MUST 提供明确 `window_start` / `window_end`；
- MUST 使用独立 request id / idempotency key；
- MUST NOT 属于默认 daily run；
- MUST NOT 改变正常 monitoring pool 的历史语义。

完整、合法、schema 正确、pagination 完成的 `0 records` 响应：

- MAY 作为成功的 provider response；
- SyncRun MAY 成功，窗口的 normalized record count 为 0（具体统计字段由 4.2B 在不改变语义的
  前提下确定）；
- MUST NOT 因 absence 删除、cancel 或修改已有 EarningsEvent；
- MUST NOT 伪造 candidate 或 review observation。

异常缩减 MAY 在 4.2C / 4.2F 作为 operational warning / metric 设计，但：

- MUST NOT 把 "0 records" 本身定义为 domain failure；
- MUST NOT 把历史非空直接推导为当前数据缺失；
- MUST NOT 参与 canonical identity 或 status mutation；
- 若未来引入 anomaly threshold，MUST 是独立可配置的 operational policy。

### 6. Duplicate / merge / canonical collision（4.2A-05）

Stage 4.2 批准 **No destructive merge**：

- MUST NOT 删除 loser；
- MUST NOT copy loser 历史到 winner；
- loser MUST 保留为 candidate / review-only historical row；
- provider observations MUST 通过 append-only reconciliation decision / mapping 指向 winner
  canonical event；
- loser 的 SourceEvidence、DataChange、schedule history、status history MUST 原样保留；
- 旧 decision MUST NOT 更新，通过 `supersedes` 链产生新 decision；
- replay MUST 使用最新有效 decision；
- mapping 冲突 MUST fail closed；
- loser MUST NOT 进入未来公开 canonical selector。

Canonical collision：

- MUST NOT 自动 merge；
- MUST NOT delete；
- MUST NOT overwrite；
- MUST 写 collision / review decision；
- 等待人工或未来已批准规则解决。

### 7. Provider / license gate（4.2A-06）

Stage 4.2A-4.2E 全部保持 provider-neutral、fixture-first。4.2F 之前 MUST NOT：

- 实现真实 provider adapter；
- 写真实 key；
- 下载或提交真实第三方 calendar fixture；
- 做 production sync；
- 对外展示第三方数据。

4.2F 开始前 MUST 存在人工完成的 provider / license checklist，至少记录：

- provider 名称；
- product / plan；
- Terms URL / version / effective date；
- API 使用许可；
- caching 权；
- persistence 权；
- redistribution / public display 权；
- notification / alert use 权；
- derived data 权；
- self-host / server-side use 权；
- rate limits；
- retention restrictions；
- reviewer；
- reviewed_at；
- conclusion：approved / rejected / restricted。

checklist 未完成时，4.2F MUST 保持 BLOCKED。本 ADR 不选择最终 provider。

### 8. Normalized provider record contract

parser MUST 输出 provider-neutral record，至少包含：

- `provider_event_id`
- `raw_data_record_id`
- `raw_position`
- `cik`
- `ticker`
- `exchange`
- `provider_symbol`
- `company_name`
- `fiscal_label_raw`
- `fiscal_year`
- `period_end_date`
- normalized `period_type`
- `fiscal_calendar_type`
- `period_length_weeks`
- `estimated_release`
- `release_precision`
- `release_session`
- `source_observed_at`
- `confidence`
- `parser_version`

parser boundary：

- ONLY 解析和规范化；
- MUST NOT 做 company match；
- MUST NOT 决定 canonical period identity；
- MUST NOT 创建 ORM domain object；
- MUST NOT 创建 SourceEvidence；
- missing MUST NOT 被猜测为具体值；
- Q4 -> FY 归一 ONLY 复用既有 identity normalization contract。

### 9. Observation / Decision persistence direction（4.2B planned）

4.2B 计划新增以下模型；4.2A MUST NOT 创建 model 或 migration。

#### `EarningsCalendarObservation`

职责：

- provider-neutral normalized revision；
- 保存 provider external identity 与 raw lineage；
- 支撑 replay 与 reconciliation。

预期唯一性：

```text
(raw_data_record, parser_version, provider_event_id)
```

MUST 支持 `(source, provider_event_id)` 的高效查询。

#### `EarningsReconciliationDecision`

职责：

- append-only decision history；
- review / collision / mapping / dedup / conflict 的结构化事实；
- MUST NOT 把结构化 match factors 塞进 AuditRecord JSON。

至少保留：

- `observation`
- `decision_type`
- `status`
- `target_event` nullable
- `rule_version`
- `match_factors`
- `reason`
- `actor` / `sync` context
- `decided_at`
- `supersedes`
- deterministic `decision_key`
- 可由 decision 表达的 covered fields（用于 manual authority）

契约语义：

- decision MUST be append-only；
- 新 decision MAY supersede 旧 decision；
- 旧 decision MUST NOT 被改写或删除；
- "最新有效 decision" MUST 是可查询、可重放、可审计的；
- `decision_key` MUST 由规范化、无凭据的不可变输入生成，至少覆盖 observation identity、
  decision_type、target_event、rule_version 与来源/actor/request identity，MUST NOT 包含
  `decided_at`；
- 同一 key 重放 MUST 复用原 decision；
- manual decision MUST 记录 actor_user、reason、request_id，并在同一事务写入 AuditRecord；
- review_required decision MUST 至少能以 decision_type / reason / match_factors 区分：
  identity ambiguous、field conflict、low confidence、provider collision、unsupported mapping。

各操作的 audit expectation：

- provider record persisted：raw / observation lineage；未匹配时 MUST NOT 创建 SourceEvidence；
- candidate created：SourceEvidence + AuditRecord(create)，初始值不伪造 DataChange；
- schedule updated：DataChange + EarningsDateChange + AuditRecord；
- candidate promoted：每个 identity 字段的 DataChange + 一条 operation-level AuditRecord；
- duplicate / collision / manual decision：append-only decision + AuditRecord；
- ignored / rejected record：append-only decision + AuditRecord。

4.2B MUST 评估是否需要扩展 AuditRecord target enum 以覆盖 observation / decision；该扩展属于
planned schema change，4.2A 不实现。

### 10. Candidate creation contract

未来 candidate creation 的 owner MUST 是独立 earnings domain service。

MUST：

1. preallocate EarningsEvent UUID；
2. 先创建可验证 target UUID 的 SourceEvidence；
3. 同一事务创建 candidate；
4. 初始 `identity_status = candidate`、`identity_key = NULL`、
   `identity_rule_version = NULL`、`status = scheduled_estimated`；
5. 即使 identity facts 完整，也先创建 candidate，再显式 promotion；
6. schedule ONLY 通过 `update_earnings_schedule` 写入；
7. canonical ONLY 通过 `promote_earnings_event` 产生；
8. MUST NOT 自动 confirm / released / cancelled；
9. 初始 create MUST 写 AuditRecord；
10. 初始值 MUST NOT 伪造 DataChange；
11. 单个 provider record MUST 使用短事务；
12. HTTP / network MUST NOT 进入 transaction。

此外：

- candidate 的 fiscal metadata ONLY 来自 parser facts；缺失 MUST 保持 NULL；
- candidate creation 重放 MUST 由 observation / decision 唯一键防重，MUST NOT 创建第二个
  candidate。

### 11. Monitoring pool、scope 与 replay

Stage 4.2 monitoring pool 定义为：

> 公司存在 as-of 有效 SecurityListing，并且该 listing 处于任一 enabled index 的 normative
> IndexMembership 中。

必须新增稳定 selector contract：

```text
earnings_monitoring_pool(as_of_date)
monitoring_pool_hash(...)
```

SyncRun scope MUST 保存：

- `monitoring_pool_as_of`
- `monitoring_pool_hash`
- `selector_version`

MUST NOT 在 scope 保存完整 company ID 列表。

Replay MUST：

- 使用原 run 的 as-of 重新计算 pool；
- hash 不一致时 integrity failure；
- MUST NOT 悄悄使用"今天的实时池"。

Stage 5 未来 ONLY 扩展 selector 为 `index monitoring pool OR active WatchlistItem`，MUST NOT
改变 4.2 调用方契约。

### 12. Pagination / partial

- 一个 SyncRun = 一个 logical window；
- 多 page 每页独立 RawDataRecord / RawDataObservation / ParseAttempt；
- pagination 未完整时 MUST NOT 宣称 logical window 完成；
- partial page failure MUST 保留已成功 raw lineage；
- 默认在完整 pagination 前 MUST NOT 做该 logical window 的 domain writes；
- retry MUST 使用新 idempotency key；
- MUST NOT 删除已成功 raw history。

同步编排 MUST 先持久化 raw，再 parse，再按 provider record 使用短事务 reconciliation；
network fetch MUST NOT 进入 transaction。

### 13. Sync / replay identity

冻结：

```text
job_type = "earnings.calendar_window"
```

scope 至少：

- `capability`
- `provider_key`
- `window_kind`
- `window_start`
- `window_end`
- `monitoring_pool_as_of`
- `monitoring_pool_hash`
- `selector_version`

scheduled run idempotency identity MUST 稳定包含：

- source
- provider
- canonical window
- pool hash
- schedule bucket

manual rerun / operator retry：

- MUST 提供显式 request id；
- 失败重试 MUST 使用新 key；
- MUST NOT 伪装成原运行。

重叠窗口：

- MUST canonicalize window；
- MUST 使用 `(source, job_type)` 级运行锁 / advisory lock；
- MUST NOT 让两个 worker 同时对同 logical scope 做 domain reconciliation。

Replay 分层：

- raw replay：同 run 同 payload 完全复用 record/observation；
- parse replay：`(observation, parser_version)` 幂等；
- normalized replay：observation 唯一键幂等；
- domain replay：最新有效 decision 指向已有 target 时 no-op。

### 14. Stage boundaries

| 阶段 | 范围 |
|---|---|
| 4.2A | ONLY contract ratification / docs；本 ADR |
| 4.2B | schema foundation：`EarningsCalendarObservation`、`EarningsReconciliationDecision`、audit target constraint changes、DB constraints / migrations / concurrency tests |
| 4.2C | fixture-first ingestion & replay：parser protocol、raw-first、pagination、scope/idempotency、empty response semantics、replay |
| 4.2D | candidate creation & company matching |
| 4.2E | reconciliation / dedup / conflict / review / manual decision authority |
| 4.2F | ONLY license gate 通过后 live provider + command |

持续边界：

- 4.3 public earnings / company UI 不属于 4.2；
- 4.4 SEC 不属于 4.2；
- 4.5 IR confirmation / conference call 不属于 4.2；
- Stage 5 watchlist 不属于 4.2；
- Stage 6 notification 不属于 4.2；
- 7.2 完整 Admin review UI 不属于 4.2。

## 结果

- provider external identity 与 canonical identity 分离，ADR-001/ADR-009 语义不变；
- 4.2 不实现 fuzzy auto-merge，所有不精确匹配进入 review；
- 第三方 calendar 的字段权限被限制在 estimated schedule，不会冒充 confirmed/released/cancelled；
- empty calendar、partial pagination 与 provider absence 都不会触发删除或取消；
- duplicate 处理不做 destructive merge，历史与 loser 完整保留；
- monitoring pool、scope、replay 与 license gate 成为 4.2B-4.2F 的稳定输入。

## 实现门

- 4.2A：ONLY 文档与 ADR；无 model / migration / code / fixture / provider；
- 4.2B：实现 observation / decision schema 与约束，不实现 provider 网络与 reconciliation 规则；
- 4.2C：实现 fixture-first parser / ingestion / replay，不做 domain reconciliation；
- 4.2D：实现 candidate creation 与 company matching，不做跨 Provider merge；
- 4.2E：实现 exact-only dedup / conflict / review / manual authority；
- 4.2F：license checklist 完成后才实现 live provider 与 command；
- 所有阶段 MUST 保持 ADR-001/ADR-007/ADR-008/ADR-009 的既有语义。

## 仍待确认

以下项目不阻塞 4.2B：

- 4.2F 最终 provider 选择与 license checklist 结论；
- anomaly shrink 的 operational warning 阈值，仅作为非 domain 的运维策略；
- 4.4 / 4.5 更高 authority 来源的字段级冲突矩阵；
- 7.2 review UI、复核负责人与 SLA；4.2E 只要求可查询 service 与 fail-closed 语义；
- `decision_type` / `status` 的最终枚举字符串；语义已冻结，具体值属于 4.2B 实现细节。
