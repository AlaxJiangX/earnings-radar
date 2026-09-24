# ADR-013：Earnings Candidate & Company Matching Contract

- 状态：已接受
- 日期：2026-09-25
- 决策者：产品负责人
- 影响阶段：4.2D-2 Candidate Creation & Company Matching
- 评审基线：`origin/main`，commit `49c3d90ec1521b6fd762418115ca97e6a1f7265e`

## 1. 背景

Stage 4.2D-1 已实现 Company-level monitoring-pool selector：

```text
canonical unit = Company
snapshot = immutable historical fact
as_of = America/New_York business date
selector_version = earnings-monitoring-pool-v1
```

Stage 4.2C 已完成 normalized earnings-calendar observation 与 offline replay。当前还缺一层：

```text
normalized observation
-> 在 frozen monitoring pool 中匹配 Company
-> 创建可审计的 Candidate
```

Candidate 不能重新运行 selector，不能跨 Provider 合并，不能决定 canonical event identity，
也不能写最终 EarningsEvent schedule/status。上述工作仍属于 4.2E/4.2F。

## 2. 结论摘要

| 问题 | 决策 |
|---|---|
| Candidate domain object | 现有 `EarningsEvent(identity_status=candidate)` |
| canonical matched unit | `Company` |
| monitoring scope | `MonitoringPoolSnapshot.members` |
| snapshot -> SyncRun linkage | 由 scope 的三个 frozen fields 唯一定位，不需要新 FK |
| matching date | `monitoring_pool_as_of` |
| matching strategy | exact CIK，或 exact ticker + canonical exchange |
| name / provider symbol fallback | 不允许 |
| ambiguous / unmatched / out-of-pool | append-only decision；不创建 candidate |
| match / candidate identity | execution/result key for every outcome；candidate UUID only for `MATCHED` |
| matcher version | `earnings-company-match-v1` |
| matching input revision | Required: YES |
| candidate hash | Not needed |
| schema change | Not required |
| Provider-specific exchange mapping | 4.2F adapter responsibility |
| retry/replay | 只读取 frozen snapshot；不调用 selector |

## 3. Candidate 定义

Candidate 沿用 ADR-009/ADR-010 已批准的 `EarningsEvent` candidate：

```text
EarningsEvent
  identity_status = candidate
  identity_key = NULL
  identity_rule_version = NULL
  status = scheduled_estimated
```

Candidate 表示：

> 一条 normalized earnings observation 在一个 frozen monitoring-pool snapshot 中，
> exact match 到唯一 Company，并已建立 SourceEvidence 与 matching decision 的候选事件。

Candidate 不是新的 Company-match 表，也不是 canonical EarningsEvent。创建 candidate 仍要求
显式 promotion 才能进入 canonical identity 流程。

## 4. 分层与职责边界

```text
RawDataRecord
  -> EarningsCalendarObservation
  -> EarningsReconciliationDecision:company_match
  -> EarningsEvent candidate
  -> later reconciliation / promotion
  -> canonical EarningsEvent
```

Candidate matching 只回答：

```text
which Company does this normalized observation belong to?
```

它 MUST NOT：

- 运行或重算 monitoring-pool selector；
- 把 out-of-pool Company 加入 snapshot；
- 做 cross-Provider candidate merge；
- 决定 canonical `period_end_date + period_type` identity；
- 选择 Provider precedence；
- 修改现有 EarningsEvent；
- 做 reconciliation decision 或 manual authority；
- 发送通知。

## 5. Monitoring Pool Scope 与 Snapshot Resolution

`RATIFIED DECISION`

Candidate search space 的 authorization boundary 是当前 run 的 frozen
`MonitoringPoolSnapshot`：

```text
candidate company search space = snapshot.members
```

Snapshot resolution：

```text
SyncRun.scope.monitoring_pool_as_of
+ SyncRun.scope.selector_version
+ SyncRun.scope.monitoring_pool_hash
-> MonitoringPoolSnapshot unique identity
```

`MonitoringPoolSnapshot` 已对
`(as_of_date, selector_version, pool_hash)` 设置唯一约束，因此现有 contract 足以确定唯一
snapshot。MUST NOT 新增 `SyncRun.monitoring_pool_snapshot` FK 来替代这项校验。

如果 scope 缺字段、snapshot 不存在、snapshot 重复或 snapshot integrity 校验失败：

```text
hard fail
```

Candidate decision 的 canonical match factors MUST 保存 snapshot id、as-of、selector
version、pool hash 和 snapshot input revision，从而可以回答该 match 使用了哪个 snapshot。

## 6. Matching Inputs

`FACT`

当前 `EarningsCalendarObservation` 可提供的匹配事实：

| Input | Status | Reliability | Use |
|---|---|---|---|
| `cik` | AVAILABLE | provider hint；非空时按 exact CIK 处理 | Tier 1 exact |
| `ticker` | AVAILABLE | provider-specific；strip + uppercase | Tier 2 exact |
| `exchange` | AVAILABLE BUT PROVIDER-SPECIFIC | adapter 必须输出 canonical exchange | Tier 2 exact |
| `provider_symbol` | AVAILABLE BUT PROVIDER-SPECIFIC | 不作为 generic match key | evidence only |
| `company_name` | AVAILABLE | 不可靠名称 | evidence only；不匹配 |
| MIC | MISSING | SecurityListing 当前没有 MIC | 不参与 V1 |
| country / security type / currency | MISSING | 未持久化 | 不参与 V1 |
| earnings dates / fiscal facts | AVAILABLE | 不是 Company match key | candidate facts |

Provider adapter 对 exchange/ticker 的规范化是 prerequisite。Generic matcher 只接受已规范化
的 provider-neutral 输入，不实现 Provider-specific alias table。

## 7. Matching Temporal Rule

`RATIFIED DECISION`

```text
matching_date = monitoring_pool_as_of
```

原因：

- snapshot 的 temporal contract 固定在 pool as-of；
- monitoring pool membership 与 listing basis 都由该日期定义；
- provider earnings date 不是稳定的 historical identity；
- `source_observed_at` 可能缺失或属于 Provider 观察时间，不是 snapshot scope。

Listing lookup 使用：

```text
effective_from <= monitoring_pool_as_of < effective_to
```

Candidate matching MUST NOT 读取 current wall clock、`MarketIndex.is_enabled` 或 current
monitoring status。

## 8. Matching Strategy

`RATIFIED DECISION`

### Tier 1: exact CIK

```text
normalized observation CIK
==
snapshot member Company.cik
```

CIK 非空时只接受 exact normalized value。

CIK 的真实 schema 语义：

- `Company.cik` nullable；
- 非 NULL 值由数据库 unique constraint 保证唯一；
- CIK 不是 effective-dated 字段，允许通过受审计的 Company update 变更；
- matcher 使用 matching 时 persisted 的 normalized CIK；
- CIK 变更会使 matching input revision 改变，形成新的 matching revision，而不是改写旧
  candidate；
- 如果数据损坏导致多个 in-pool Company 出现同一 normalized CIK，必须 `AMBIGUOUS`，不得
  任意选择。

### Tier 2: exact ticker + exchange

```text
normalized observation ticker
==
as-of listing ticker
AND
normalized observation exchange
==
as-of listing exchange
```

Listing MUST 属于 snapshot member Company，且必须在 `monitoring_pool_as_of` effective。

Snapshot member basis 是 Company 入选 pool 的 provenance。Ticker/exchange match MAY 使用该
member Company 在 as-of 有效的其他 SecurityListing；这不扩大 Company pool，也不改写
snapshot basis。匹配证据必须记录实际命中的 SecurityListing。

### Forbidden fallback

MUST NOT 使用：

- fuzzy company name；
- ticker-only match；
- `provider_symbol` 直接当 ticker；
- 当前 Provider response；
- 当前 MarketIndex enablement；
- DB default ordering 或 arbitrary first match。

如果两个 tier 同时返回 Company，必须合并 Company identity：

- 指向同一 Company：允许；
- 指向不同 Company：`AMBIGUOUS`。

同一 Company 多个 listing 命中不构成歧义；`SecurityListing` 只是 matching evidence。

## 9. Exchange / MIC Boundary

`RATIFIED DECISION`

V1 generic matcher 的 canonical key 是：

```text
(uppercase ticker, uppercase canonical exchange)
```

当前 SecurityListing 没有 MIC 字段，因此 V1 不把 MIC 作为必需输入。

Provider-specific exchange alias 处理属于 adapter：

- Provider adapter MUST 把 upstream exchange 规范化为 internal canonical exchange；
- generic matcher MUST NOT hardcode `XNAS`/`NMS`/`NASDAQ` 等 Provider mapping；
- unknown exchange 不是临时猜配理由，按 `UNMATCHED` 或 `OUT_OF_POOL` 处理；
- Provider 无法提供 canonical exchange 或 CIK 时，只保留 observation，不创建 candidate。

## 10. Monitoring Pool Constraint

`RATIFIED DECISION`

匹配首先限制到 frozen snapshot members。Exact identifier 如果在数据库中找到 Company，但该
Company 不在 snapshot：

```text
OUT_OF_POOL
```

不得：

- 自动加入 snapshot；
- 用 current pool 替换 snapshot；
- 用 current Company monitor status 扩大范围。

## 11. Match Outcomes 与 Candidate Status

`RATIFIED DECISION`

候选匹配状态使用最小四分法：

| Match status | Meaning | Persistence |
|---|---|---|
| `MATCHED` | exact match 唯一指向一个 in-pool Company | 创建 `EarningsEvent(candidate)` + decision + SourceEvidence |
| `UNMATCHED` | 没有可用 exact match | 不创建 candidate；append-only `no_match` decision |
| `AMBIGUOUS` | 多个 valid match，或多 tier 冲突 | 不创建 candidate；append-only `review_required` decision |
| `OUT_OF_POOL` | 有 exact Company/listing match，但不在 frozen snapshot | 不创建 candidate；append-only `ignored` decision |

Candidate 只表示 `MATCHED` 且已归属 Company 的结果。Unmatched / ambiguous / out-of-pool
不是 candidate 状态，而是 matching decision outcome，因此不需要扩展 Candidate workflow。

## 12. Candidate Persistence Decision

```text
Schema change required = NO
```

复用现有模型：

- `EarningsEvent`：candidate row；
- `EarningsReconciliationDecision`：append-only company match provenance；
- `SourceEvidence`：candidate 与 raw evidence 的链接；
- `AuditRecord`：candidate/decision 操作审计。

不新增 `EarningsCandidate` 表，也不在 `SyncRun` 增加 snapshot FK。

`EarningsReconciliationDecision` 的 mapping：

| Match outcome | decision_type | status | target_event |
|---|---|---|---|
| `MATCHED` | `created_candidate` | `resolved` | candidate EarningsEvent |
| `UNMATCHED` | `no_match` | `rejected` | NULL |
| `AMBIGUOUS` | `review_required` | `open` | NULL |
| `OUT_OF_POOL` | `ignored` | `rejected` | NULL |

Decision 的 `match_factors` 使用受控 `company_match` namespace，避免与后续 reconciliation
字段混用。

现有 `EarningsReconciliationDecision.decision_key` unique constraint 是 match execution
幂等的数据库防线；`EarningsEvent.id` 是 deterministic candidate row identity。两者配合可
保证并发 same-match 不产生第二条 equivalent candidate/decision，无需新增 constraint。

## 13. Candidate Creation Flow

`RATIFIED DECISION`

对 `MATCHED`：

1. 从 persisted SyncRun 与 observation 重新加载上下文；
2. resolve frozen MonitoringPoolSnapshot；
3. 计算 canonical match identity 与 deterministic candidate UUID；
4. 创建 candidate-target `SourceEvidence`；
5. 创建 `EarningsEvent(identity_status=candidate)`；
6. 写 create AuditRecord；
7. 写 `created_candidate` decision；
8. fiscal metadata 只使用 observation facts；`fiscal_calendar_type = NULL` 必须映射为
   `UNKNOWN`，不得推断为 `MONTH_BASED`；schedule fact 如存在，必须通过既有 schedule
   service，不直接写日期字段。

`UNKNOWN` 只表示来源未提供该事实，不进入 matching revision、execution key、candidate
identity 或 canonical event identity。已有显式 fiscal calendar fact 保持不变；历史
`month_based` 行因无法区分旧默认值与显式事实，不在本 repair 中重写。

对 `UNMATCHED` / `AMBIGUOUS` / `OUT_OF_POOL`，不创建 EarningsEvent 或 SourceEvidence，
只写 append-only decision 与 AuditRecord。

所有步骤在单个短 transaction 内完成；HTTP / Provider call MUST NOT 进入 transaction。

并发 same-match 产生的 candidate PK 或 decision unique 冲突必须使用 nested savepoint 回滚，
离开 broken transaction 后重新加载 winning candidate/decision 并验证内容；不得在事务失败
状态中继续查询，也不得覆盖旧 candidate。

## 14. Match Execution / Result / Candidate Identity

`RATIFIED DECISION`

必须区分三种 identity：

```text
match_execution_key =
  SHA256(matcher_version + observation_id + snapshot semantic identity + matching_input_revision)

match_result_key =
  SHA256(match_execution_key + normalized outcome + matched/outside evidence)

candidate_identity_key =
  SHA256(match_execution_key + matched Company + canonical matched SecurityListing evidence)
```

`match_execution_key` 和 `match_result_key` 对所有 outcome 都存在，用于 append-only decision
identity。`candidate_identity_key` 只在 `MATCHED` 时产生。

候选 UUID 使用版本化、代码固定的 deterministic derivation，例如以固定 UUID namespace 加
`candidate_identity_key` 做 UUIDv5。不得把随机 UUID、`created_at`、DB insertion order 或
match execution 的内存状态当 candidate identity。

同一 matcher contract、observation、snapshot 与 historical matching facts 重复执行必须得到
同一 match result；若结果为 `MATCHED`，还必须得到同一 candidate UUID。

## 15. Matcher Version

`RATIFIED DECISION`

```text
EARNINGS_COMPANY_MATCHER_VERSION = "earnings-company-match-v1"
```

MUST bump：

- matching key 或 normalization 变化；
- CIK / exchange+ticker 的 precedence 或冲突规则变化；
- temporal lookup 变化；
- snapshot scope 约束变化；
- ambiguity 或 out-of-pool 语义变化；
- candidate identity 输入变化。

MUST NOT bump：

- pure refactor；
- query optimization；
- logging、typing、注释或非语义测试变化；
- 不改变匹配输出的 storage/index 调整。

Unknown matcher version MUST fail closed，不得回退到 v1。

## 16. Matching Input Revision

```text
required = YES
```

`matching_input_revision` 是 canonical JSON 的 SHA-256，至少包含：

```text
matcher_version
observation_id
normalized CIK / ticker / exchange
frozen snapshot id
snapshot as-of / selector_version / pool_hash / input_revision
snapshot member Company ids
matching-date Company CIK facts
in-pool matching-date SecurityListing ids / ticker / exchange / effective intervals
exact out-of-pool Company/listing facts used for OUT_OF_POOL classification
```

MUST NOT 包含：

```text
current wall clock
DB insertion order
Python hash()
Provider availability
current MarketIndex.is_enabled
current Company.monitoring_status
```

同一 matching facts 必须得到同一 revision；historical query/list facts correction 必须改变
revision，从而形成新的 candidate revision 而不是覆盖旧 candidate。

## 17. Match Evidence

`match_factors` 的 `company_match` namespace 至少保存：

```text
matcher_version
match_execution_key
matching_input_revision
match_result_key
match_status
match_strategy
monitoring_pool_as_of
monitoring_pool_snapshot_id
monitoring_pool_hash
selector_version
normalized cik
normalized ticker
normalized exchange
provider_symbol
company_name
matched_company_id
matched_security_listing_ids
matched_outside_pool_company_ids
reason_code
```

不得保存 raw payload、完整 Provider response、secret 或大段 company/listing dump。

## 18. Candidate / Decision Audit

`RATIFIED DECISION`

- `MATCHED` candidate creation MUST 在同一事务创建 SourceEvidence、candidate 和
  operation-level AuditRecord，并写 `created_candidate` decision。
- `UNMATCHED` / `AMBIGUOUS` / `OUT_OF_POOL` MUST 写 decision 与 AuditRecord，但不创建
  candidate 或 SourceEvidence。
- `EarningsEvent.source_evidence` 在 candidate creation 时可指向新 evidence；promotion 不得
  改写该值。
- Decision 必须能回答：哪条 observation、哪个 snapshot、哪个 matcher version、哪些 listing
  facts、哪个 Company，以及为何 matched/unmatched/ambiguous/out-of-pool。

## 19. Replay / Re-run Semantics

`RATIFIED DECISION`

Candidate matching re-run/replay 必须复用 frozen snapshot，MUST NOT 调用 selector。

同一：

```text
normalized observation
+ snapshot
+ matcher version
+ matching_input_revision
```

必须得到同一匹配结果、同一 decision identity 和同一 candidate（若 MATCHED）。

如果 replay 产生新的 normalized observation revision（例如 parser version 改变）：

- 新 observation 可以产生新的 candidate lineage；
- 旧 candidate 必须保持；
- 不得覆盖旧 match evidence。

如果 SecurityListing / Company matching facts correction 改变 `matching_input_revision`：

- 必须创建新的 matching decision；
- 如果 MATCHED，新的 candidate revision 使用新 identity；
- 旧 candidate 与其 decision 保留；
- 不得修改旧 candidate 以“修正”历史匹配。

## 20. Candidate Creation Trigger 与 Ownership

`RATIFIED DECISION`

Candidate creation 是 normalization 后的独立 use case，不能藏在 parser 或 Provider：

1. scheduled ingestion 完成全部 pagination 与 normalized persistence 后；
2. offline replay 只对完整、可验证的 source/replay evidence 执行；
3. 继续使用当前 `(source, job_type)` ownership；
4. 单条 observation 使用短 transaction；
5. 不创建新的 queue、scheduler 或 lock system；
6. partial pagination 不进入 candidate creation。

如果当前 run 已经明确 terminal partial/failed，不得补写 candidate。

Candidate phase MUST 在 run terminal finalization 之前运行，同时在现有 ownership 内完成。
如果现有 lifecycle 在返回前已经 finalize，4.2D-2 implementation 必须增加 pre-finalize
integration point，不能在 ownership 释放后把 candidate failure 伪装成已成功 run 的后续步骤。

Candidate matching 的单个业务结果 `UNMATCHED` / `AMBIGUOUS` / `OUT_OF_POOL` 不是 run failure；
只有 snapshot、ownership、persistence、integrity 等技术失败才使 run partial/failed。

## 21. Failure Semantics

| Condition | Behavior |
|---|---|
| observation 缺 CIK 且缺 ticker/exchange | `UNMATCHED` decision |
| ticker-only，无 exchange | `UNMATCHED` decision |
| unknown exchange / no matching listing | `UNMATCHED` decision |
| exact match 唯一且 in pool | candidate `MATCHED` |
| exact match 唯一但 out of pool | `OUT_OF_POOL` decision |
| multiple Companies / conflicting tiers | `AMBIGUOUS` decision；no candidate |
| snapshot missing / scope invalid | hard fail |
| snapshot integrity failure | hard fail |
| unknown matcher version | hard fail |
| candidate/decision persistence conflict | fail closed，reload winner，不覆盖历史 |

不得把任何数据错误静默降级为空候选池。

## 22. Existing Data Sufficiency

| Required fact | Status | Evidence | Gap |
|---|---|---|---|
| normalized CIK | AVAILABLE | `EarningsCalendarObservation.cik` | 空值需要 fallback |
| normalized ticker | AVAILABLE | `ticker` | provider-specific spelling |
| canonical exchange | AVAILABLE BUT PROVIDER-SPECIFIC | `exchange` | adapter normalization |
| MIC | MISSING | 无字段 | 不作为 V1 prerequisite |
| Company stable identity | AVAILABLE | `Company.id` | 无 |
| Company CIK | AVAILABLE | `Company.cik` | 历史 CIK correction 仍属 Company 主数据决策 |
| SecurityListing stable identity | AVAILABLE | `SecurityListing.id` | 无 |
| listing temporal validity | AVAILABLE | `effective_from` / `effective_to` | 无 |
| frozen pool membership | AVAILABLE | `MonitoringPoolSnapshot` / `MonitoringPoolMember` | 无 |
| Provider identifier mapping | MISSING | 4.2F provider gate | generic matcher 不使用 provider_symbol |
| Company names | AVAILABLE | `display_name` / `legal_name` | 不可靠，不参与 match |

## 23. Implementation Test Matrix

下一实现阶段至少覆盖：

1. exact CIK -> in-pool Company；
2. exact ticker + exchange -> in-pool Company；
3. same ticker different exchange 不误配；
4. ticker rename 按 `monitoring_pool_as_of` 解析历史 listing；
5. multiple listings same Company -> one Company；
6. multiple Companies match -> AMBIGUOUS；
7. CIK 与 ticker/exchange 冲突 -> AMBIGUOUS；
8. exact match outside snapshot -> OUT_OF_POOL 且无 candidate；
9. missing ticker / exchange -> UNMATCHED decision；
10. empty snapshot -> deterministic no-candidate behavior；
11. effective interval start/end boundary；
12. deterministic matching input revision；
13. different matcher version -> new candidate revision；
14. same matcher/input -> reuse candidate；
15. listing correction -> new input revision / candidate revision；
16. replay same normalized observation -> no duplicate candidate；
17. partial run -> no candidate；
18. concurrent same match -> one candidate / one decision；
19. decision唯一冲突后重新加载 winner，不覆盖旧历史；
20. Candidate creation 不写 EarningsEvent canonical identity / 不调用 promotion；
21. candidate creation 不调用 selector；
22. retry 使用 original snapshot，不重新 selector。

## 24. Non-Goals

- 不实现 matcher service；
- 不新增 model / migration；
- 不做 fuzzy name matching；
- 不做 Provider-specific exchange mapping；
- 不决定 provider query projection；
- 不做 cross-Provider dedup / merge / reconciliation；
- 不提前 promotion；
- 不写 schedule/status domain workflow；
- 不实现 UI、command、Celery、Redis 或 queue。

## 25. Open Decisions / Deferred

- 真实 Provider 的 exchange/MIC alias normalization 属于 4.2F adapter；
- 是否需要 MIC-first matching 或 provider identifier mapping，等 Provider 确定后处理；
- Company CIK 历史 correction 的正式主数据规则仍需后续确认；
- candidate decision 的 retention / archive 沿审计保留策略处理；
- fuzzy matching 不进入 V1，除非另有 ADR。

这些开放项不阻塞 provider-independent、fixture-first 的 4.2D-2 implementation。

## 26. Gate Decision

```text
PASS — candidate/matching contract is implementable
```

理由：

- canonical matched unit 已确定为 Company；
- frozen snapshot 可由现有 SyncRun scope 唯一解析；
- CIK 与 ticker+exchange 足以建立 provider-neutral exact tiers；
- unmatched / ambiguous / out-of-pool 有确定且可审计的 decision 语义；
- 现有 EarningsEvent / EarningsReconciliationDecision / SourceEvidence / AuditRecord
  足以承载 candidate 与 provenance；
- replay/correction/versioning 有 deterministic identity 与 append-only revision 语义；
- 不需要新的 schema、Provider mapping 或 reconciliation 才能实现 core。
