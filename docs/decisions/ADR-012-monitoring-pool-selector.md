# ADR-012：Monitoring-Pool Selector Contract

- 状态：已接受
- 日期：2026-09-24
- 决策者：产品负责人
- 影响阶段：4.2D-1 Monitoring-Pool Selector & Snapshot Foundation
- 评审基线：`origin/main`，commit `a41fef1541f6b0962cefd12116d00c3b15edea04`

## 1. 背景

Stage 4.2C 已经完成 earnings calendar ingestion、run ownership、scheduled/retry identity 和
offline replay。SyncRun scope 已持久化：

```text
monitoring_pool_as_of
monitoring_pool_hash
selector_version
```

但当前没有服务真正计算这三个字段。4.2C replay 已由 ADR-011 选择 Option A：只验证 source
run 已持久化的 pool contract，不重新选择 pool。

本 ADR 冻结 Stage 4.2D-1 的 selector contract。目标不是实现一个通用过滤框架，而是保证：

```text
同一 as-of + selector_version + 同一历史输入
-> 同一 Company 集合
-> 同一 canonical order
-> 同一 monitoring_pool_hash
```

Selector 不得依赖 Provider、candidate、reconciliation、EarningsEvent 或当前 wall clock。

Contract priority：

```text
ADR-011 supersedes ADR-010 regarding offline replay pool recomputation.
ADR-012 must not reintroduce live selector execution into offline replay.
```

## 2. 结论摘要

| 问题 | 决策 |
|---|---|
| canonical selector unit | `Company` |
| Stage 4.2 universe source | enabled `MarketIndex` 的 as-of normative `IndexMembership` |
| Stage 5 watchlist | DEFERRED；不得在本阶段改变 selector 调用契约 |
| `monitoring_pool_as_of` | `America/New_York` 业务自然日；半开区间 as-of date |
| `selector_version` | selector 业务规则与 canonical output/hash contract 的版本 |
| Provider query identity | selector 不负责；属于后续 Provider projection |
| monitoring pool hash | SHA-256 canonical JSON；包含 selector context、input revision 和成员 basis |
| historical recomputability | Hybrid |
| snapshot persistence | Required: YES |
| late-arriving correction | 不改写旧 snapshot；生成新的 input revision / snapshot |
| retry | 复用 original run 的 pool contract，不重选 |
| offline replay | 只验证 persisted source pool contract，不重选 |
| empty pool | 输入完整时的 valid deterministic result；输入缺失或损坏不得伪装为空 |
| schema change | Required: YES；仅新增 snapshot/member schema |
| candidate / reconciliation | 不在 selector 内发生 |

## 3. Selector Responsibility 与 Canonical Unit

### 3.1 定义

> Monitoring-Pool Selector 在给定 `as_of`、`selector_version`、enabled index policy 和一致读
> 输入下，从已有 Company / SecurityListing / IndexMembership 数据中确定本轮 earnings
> calendar ingestion 应关注的 Company 集合，并生成可审计、确定性的 pool snapshot 与 hash。

```text
Selector selects: Company
```

### 3.2 为什么是 Company

`RATIFIED DECISION`

- ADR-010 已将 Stage 4.2 monitoring pool 定义为公司级结果；
- EarningsEvent、candidate 和 reconciliation 的核心 identity 都以 Company 为边界；
- SecurityListing 是 as-of eligibility basis，不是用户监控对象；
- 同一 Company 的多 listing、多 share class、多指数命中必须去重成一个成员；
- Company UUID 是内部稳定 identity，不随 ticker rename、exchange change 或 listing successor
  改变。

IndexMembership 仍必须保持 SecurityListing 粒度。Selector 只是在输出层按 Company 聚合，不能
把底层 membership 改写成 Company 级关系。

## 4. Universe Source 与 Eligibility

### 4.1 Stage 4.2 universe

`RATIFIED DECISION`

Stage 4.2 的 universe source 仅为：

```text
enabled MarketIndex
-> normative IndexMembership as of monitoring_pool_as_of
-> effective SecurityListing as of monitoring_pool_as_of
-> Company
```

公司进入 pool 的充分条件：

```text
Company
  has at least one SecurityListing
  effective_from <= as_of < effective_to-or-infinity
  and that listing has at least one IndexMembership
  whose index is in the explicit enabled_index_codes input
  and whose normative effective interval contains as_of
```

### 4.2 明确不使用的输入

`RATIFIED DECISION`

- `Company.monitoring_status` 不参与 selector。它是 current-only 派生状态，不能作为历史
  as-of 事实。
- WatchlistItem 不参与 Stage 4.2 selector。其 schema 尚未实现，Stage 5 才扩展 universe。
- candidate、reconciliation decision、EarningsEvent 和通知状态不参与 selector。
- Provider 当前可用性、Provider response 或 `provider_symbol` 不参与 selector。

### 4.3 Manual override

`FACT`

当前没有 per-company manual include/exclude model。`MarketIndex.is_enabled` 是 index policy，
不是 Company override。

4.2D-1 MUST NOT 为了未来可能的 override 需求提前引入复杂 override engine。若未来需要
per-company override，必须新增显式、effective-dated 且有审计的 ADR 与 schema。

`DEFERRED`

per-Company manual override 不在 4.2D-1 scope。

## 5. as-of Contract

### 5.1 类型与语义

`RATIFIED DECISION`

```text
monitoring_pool_as_of = date
timezone context = America/New_York
```

它是用于 temporal membership 判断的美国市场业务自然日，不是：

- UTC timestamp；
- intraday evaluation instant；
- database `created_at`；
- Provider observation time。

Selector 本身不得读取 current wall clock。调用方必须显式传入 `as_of`。

### 5.2 半开区间

所有 listing 与 membership 有效期统一使用：

```text
effective_from <= as_of < effective_to
```

`effective_to=NULL` 表示无已知结束日期。开始日包含，结束日不包含。

### 5.3 边界规则

`RATIFIED DECISION`

- 未来 `effective_from > as_of` 的 membership 不入选；
- `effective_from == as_of` 的 normative membership 入选；
- `effective_to == as_of` 的 membership 不入选；
- 同一天新增或修正的记录，只要在读取时满足上述半开区间，就进入该次 calculation；
- 已完成的旧 calculation 不因后来 late-arriving correction 回写；
- `announced` / `active` / `ended` 是否参与由现有 normative status contract 与 effective
  interval 共同决定，status 标签本身不新增一套 as-of 算法。

## 6. selector_version Contract

### 6.1 V1 名称

`RATIFIED DECISION`

```text
EARNINGS_MONITORING_POOL_SELECTOR_VERSION = "earnings-monitoring-pool-v1"
```

该版本表达 selector 的：

- universe 与 eligibility 业务规则；
- canonical member 与 basis 结构；
- canonical ordering；
- input revision 与 pool hash serialization。

它不是 application git SHA，也不是数据库 migration 编号。

### 6.2 Bump 规则

MUST bump：

- 会改变 Company 入选条件的规则；
- enabled-index policy 的解释变化；
- dedupe、canonical unit 或 basis 语义变化；
- canonical ordering；
- input revision 输入集合；
- monitoring pool hash payload 或 encoding；
- 任何可能使同一历史输入产生不同 pool 结果的算法变化。

MUST NOT bump：

- pure refactor，且输入、成员、顺序和 hash 完全不变；
- query optimization，且行为与结果不变；
- logging、typing、测试或注释变化；
- 不改变 selector 语义的 DB index 或 storage change；
- 只是增加不参与 selector 的 Provider mapping。

DB schema change 本身不自动 bump。只有改变 selector 可见数据或结果语义时才 bump。

Unknown selector version MUST fail closed。历史 snapshot 仍可读取，但不能用 unknown version
静默回退到 v1。

## 7. Selector Inputs

### 7.1 Required inputs

- `as_of: date`
- `selector_version: str`
- `enabled_index_codes: tuple[str, ...]`

`enabled_index_codes` 必须显式传入并按 code 去重、排序。Selector MUST NOT 在执行过程中直接
读取可变化的 `MarketIndex.is_enabled` 来补默认值。

列表不得为空，且每一项必须属于 `MarketIndex.Code`。空列表是 policy error，不是 empty pool。

Scheduled caller 可以在 run 开始前读取一次当前 enabled index policy，再将该 policy 冻结为
selector input。

### 7.2 Derived inputs

- as-of effective SecurityListing；
- as-of normative enabled IndexMembership；
- Company aggregation / dedupe；
- canonical per-Company basis；
- canonical input revision；
- canonical member ordering 与 hash。

### 7.3 Forbidden inputs

MUST NOT 使用：

- `as_of` 已提供时的 current wall clock；
- Python `hash()`；
- DB default ordering 或 query plan order；
- random UUID generation 参与已有计算；
- 当前 Provider response、Provider availability 或 API key；
- `Company.monitoring_status`；
- candidate、reconciliation、EarningsEvent、notification state；
- 未版本化的 environment-specific rule；
- hidden manual state。

## 8. Selector Output

概念输出：

```text
MonitoringPoolSelection
  as_of
  selector_version
  enabled_index_codes
  input_revision
  members
  member_count
  pool_hash
```

每个 member：

```text
MonitoringPoolMember
  company_id
  basis
```

`basis` 是导致 Company 入选的 canonical membership facts，至少包含：

```text
index_code
security_listing_id
effective_from
effective_to
```

同一 Company 的多 listing、多 index basis 全部保留在 member basis 中，但 member 只出现一次。

Selector MUST NOT 输出或 hash Provider-specific symbol。canonical selection identity 与
Provider query identity 必须分层：

```text
selection identity = Company
provider query identity = Provider execution projection
```

## 9. Canonical Ordering 与 Dedupe

`RATIFIED DECISION`

- member 按 `company_id` 字符串升序；
- basis 先按 `index_code`，再按 `security_listing_id`，再按 `effective_from`；
- duplicate Company 合并为一条 member；
- duplicate basis tuple 去除；
- 空 basis 不允许出现在 selected member；
- 禁止依赖数据库返回顺序；
- Provider 返回顺序和本地查询顺序不得影响结果。

## 10. Monitoring Pool Hash Contract

`RATIFIED DECISION`

```text
monitoring_pool_hash =
  SHA-256(UTF-8(canonical_json(pool_hash_payload)))
```

canonical JSON 不使用 whitespace，object keys 排序，数组使用本文定义的 canonical order。

概念 payload：

```json
{
  "contract": "earnings-monitoring-pool-hash-v1",
  "selector_version": "earnings-monitoring-pool-v1",
  "as_of": "2026-09-24",
  "enabled_index_codes": ["DJIA", "NASDAQ100", "RUSSELL2000", "SP500"],
  "input_revision": "<64 lowercase hex>",
  "members": [
    {
      "company_id": "<uuid>",
      "basis": [
        {
          "index_code": "SP500",
          "security_listing_id": "<uuid>",
          "effective_from": "2026-01-02",
          "effective_to": null
        }
      ]
    }
  ]
}
```

Hash 不包含：

- snapshot `created_at`；
- evaluation wall clock；
- DB primary-key insertion order；
- Provider availability 或 Provider query symbol；
- candidate、reconciliation、EarningsEvent 或 notification state。

`input_revision` 是 canonical selector input manifest 的 SHA-256，只覆盖能够影响 eligibility 或
basis 的规范化字段：

```text
enabled_index_codes
+ 每个 as-of normative membership 的
  index_code + security_listing_id + effective_from + effective_to
+ 对应 SecurityListing 的
  company_id + security_listing_id + effective_from + effective_to
```

manifest 不包含 status label、source evidence、`last_verified_at`、`created_at`、DB row
ordering 或 Provider mapping。相同 manifest 必须得到相同 `input_revision`。

`input_revision` 明确进入 `pool_hash`。因此任何可能改变 eligibility/basis 的输入 revision
都会形成不同 hash，即使 Company 集合碰巧相同。Provider mapping 与不产生 eligible
membership 的数据变化不得改变 pool hash。

## 11. Historical Recomputability

选择：

```text
Hybrid
```

原因：

- SecurityListing 和 IndexMembership 是 effective-dated，支持 as-of 查询；
- MarketIndex `is_enabled` 是 current-only boolean，不是 temporal policy；
- IndexMembership correction 允许后续追加或修订历史事实；
- 仅依赖当前 DB 重算无法证明过去的 selector 当时看到了什么。

因此：

1. 每个 accepted selector calculation MUST 持久化完整 pool snapshot 与 hash；
2. snapshot 是 historical run fact 的 authoritative evidence；
3. 对新输入重新计算得到的是 current best reconstruction；
4. reconstruction 必须形成新 input revision，MUST NOT 覆盖旧 snapshot；
5. 没有 snapshot 的 pre-4.2D-1 historical as_of 不承诺 authoritative reconstruction。

## 12. Snapshot Persistence

```text
Schema change required = YES
```

本 ADR 不创建 model 或 migration。4.2D-1 需要最小新增：

模型与 selector 归 `earnings` app 的 monitoring-pool module 所有。该 module 只通过公开
selector / service 读取 `companies` 与 `indexes` 的稳定 identity 和 effective-dated
membership，不修改两家上游 app 的数据，也不复制 `IndexMembership` 业务规则。

### 12.1 `MonitoringPoolSnapshot`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `as_of_date` | date |
| `selector_version` | 非空字符串 |
| `enabled_index_codes` | canonical JSON array |
| `input_revision` | 64 位小写 SHA-256 |
| `pool_hash` | 64 位小写 SHA-256 |
| `member_count` | 非负整数 |
| `created_at` | UTC |

约束：

- `(as_of_date, selector_version, pool_hash)` unique；
- snapshot append-only；
- `input_revision` 与 `pool_hash` 必须在 reload 时与 persisted members 一致；
- mismatch 必须 fail closed。

### 12.2 `MonitoringPoolMember`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `snapshot_id` | FK snapshot，PROTECT |
| `company_id` | FK Company，PROTECT |
| `ordinal` | canonical order |
| `basis` | canonical JSON array；不可变 |

约束：

- `(snapshot, company)` unique；
- `(snapshot, ordinal)` unique；
- member append-only；
- `company` 删除必须被 PROTECT。

不再新增独立 provider mapping 或 candidate model。该最小 schema 足以冻结 run fact 并解释
Company 入选 basis。

## 13. Late-Arriving / Corrected Data

`RATIFIED DECISION`

场景：

```text
9/1 selector accepted pool A
9/5 late-arriving correction changes effective data for 8/31
```

规则：

- 9/1 snapshot A 与 related SyncRun 永不改写；
- 若 correction 不改变 canonical eligibility input manifest，不产生新 pool result；
- 若 correction 改变 input revision 或 output，则 9/1 可以形成新的 reconstruction
  snapshot B；
- A 是 historical run fact，B 是 current best reconstruction；
- B 不得自动修改 A 的 SyncRun、已写 observation、candidate 或 notification；
- 使用 B 必须通过显式新 run request 或符合 retry contract 的后续运行。

## 14. Scheduled、Retry 与 Replay Integration

### 14.1 Scheduled run

`RATIFIED DECISION`

```text
select and persist snapshot/hash
-> start SyncRun with monitoring_pool_as_of / hash / selector_version
-> freeze scope
```

Selector 必须在 SyncRun 创建前完成。SyncRun 开始后，pool contract immutable。mid-run index
或 listing correction 不得改变当前 run 的 pool。

### 14.2 Retry

`RATIFIED DECISION`

Retry 复用 original source run 的：

```text
monitoring_pool_as_of
monitoring_pool_hash
selector_version
```

Retry MUST NOT 重选 pool。现有 4.2C 的 `expected_pool_hash` guard 保持。

### 14.3 Offline replay

`RATIFIED DECISION`

Offline replay continues to validate persisted source pool contract only。

Replay MUST NOT：

- 调用 selector；
- 读取 today's enabled index policy；
- 用 current DB 重算历史 pool；
- 因 late-arriving correction 产生不同 pool 后改变 source scope。

ADR-010 中“Replay 重新计算 pool”的早期表述已由 ADR-011 Option A 取代，并由本 ADR 再次确认。

## 15. Provider Independence

`RATIFIED DECISION`

Selector 输出内部 canonical Company pool。Provider query identity 是独立 execution projection：

```text
Company pool
-> as-of active SecurityListing(s)
-> Provider adapter mapping
-> Provider request identity
```

该 projection：

- 不得改变 selector membership 或 `monitoring_pool_hash`；
- 由 Provider execution contract 定义；
- 最终 Provider 未确认，因此具体 CIK / ticker / exchange / provider symbol 规则为
  `OPEN DECISION`，属于 4.2F 前置条件；
- 不阻塞 provider-independent 4.2D-1。

Multi-listing Company 应查询 primary、all eligible listings 还是 Provider-specific mapping，
当前未决定。4.2D-1 不自行决定。

## 16. Candidate / Reconciliation Boundary

`RATIFIED DECISION`

```text
selector:
which Companies are monitored for this run

calendar ingestion:
which raw Provider facts were fetched for that run

candidate creation / company matching:
which normalized observation belongs to an in-pool Company and may become an EarningsEvent candidate

reconciliation:
which candidate maps to which canonical EarningsEvent
```

4.2D-1 不实现：

- candidate creation；
- company matching；
- reconciliation；
- EarningsEvent write；
- Provider fetch。

Candidate creation 只能消费当前 SyncRun 已冻结的 pool，不得重新选择 pool 或以 current pool
替换 run pool。

## 17. Failure 与 Empty Pool Semantics

### 17.1 Hard failures

- `as_of` 类型错误；
- unknown selector version；
- missing / invalid enabled index policy；
- required schema 或 temporal data 不一致；
- duplicate snapshot identity 但内容不同；
- snapshot reload 后 input revision / hash mismatch；
- DB read 无法形成一致 snapshot。

上述情况不得返回 empty pool。

### 17.2 Valid empty pool

`RATIFIED DECISION`

当输入完整、schema 正确且 selector 正常执行时，`0 selected Company` 是 valid deterministic
result。它仍必须生成 canonical empty members 数组和固定 hash。

Operational command MAY 在完整生产数据下对 empty pool 发出 warning/alert，但该 operational
policy 不得改变 selector domain semantics，也不得把 missing input 伪装成 empty。

## 18. Observability 与 Performance

最小 observability：

```text
selector_version
as_of
enabled_index_codes
input_revision
member_count
pool_hash
snapshot_id
```

Performance contract：

- 使用 set-based query；
- 禁止 per-Company N+1；
- 读取使用一致数据库 snapshot；
- expected universe 为四指数 Company union，规模为数千级别；
- member 写入应按单个 snapshot 批量完成；
- hash 计算不得依赖数据库顺序。

## 19. Existing Data Sufficiency

| Required fact | Status | Evidence | Gap |
|---|---|---|---|
| Company stable identity | AVAILABLE | `Company.id` UUID | 无 |
| SecurityListing effective history | AVAILABLE | `[effective_from, effective_to)` + exclusion constraints | 无 |
| IndexMembership effective history | AVAILABLE | normative statuses + as-of selectors | 无 |
| enabled index policy | AVAILABLE BUT NON-TEMPORAL | `MarketIndex.is_enabled` + audit DataChange | 不能仅靠 current row 权威重算历史 |
| Company monitoring status | AVAILABLE BUT CURRENT-ONLY | `Company.monitoring_status` | 明确不作为 selector input |
| index correction history | AVAILABLE | AuditRecord / DataChange / supersedes | reconstruction 复杂，不替代 snapshot |
| Watchlist | MISSING | Stage 5 未实现 | DEFERRED |
| pool snapshot / input revision | MISSING | 当前只有 SyncRun scope hash | 4.2D-1 schema addition |
| Provider query mapping | MISSING | Provider / license gate 未完成 | 不作为 selector dependency；4.2F |
| manual include/exclude | MISSING | 无 model | NOT IN 4.2D |

## 20. 4.2D-1 Test Matrix

实现阶段至少覆盖：

### Determinism

1. same inputs -> same members；
2. same inputs -> same input revision / hash；
3. DB insertion order change -> same hash；
4. duplicate Company/membership basis -> deterministic dedupe。

### As-of

5. `effective_from > as_of` excluded；
6. start boundary included；
7. `effective_to == as_of` excluded；
8. end-null 持续有效；
9. disabled index excluded。

### Versioning

10. selector version change 明确产生新 identity；
11. unknown selector version rejected。

### Historical

12. snapshot reload 与 persisted hash 一致；
13. late-arriving correction 不改写旧 snapshot；
14. correction 产生新 input revision / hash；
15. pre-snapshot authoritative reconstruction 不伪造。

### Mapping / Listing

16. ticker rename 不改变 Company canonical identity；
17. listing successor 使用 as-of interval；
18. 多 listing / share class Company 只产生一个 member；
19. delisted listing 在 as-of 后不参与。

### Snapshot / Hash

20. canonical order；
21. empty pool hash；
22. snapshot reuse 幂等；
23. member / basis change -> hash change；
24. corrupted snapshot / hash mismatch fail closed。

### SyncRun Integration

25. scheduled run 在开始前冻结 pool；
26. retry 复用 original pool contract；
27. replay 不调用 selector；
28. same pool identity 不创建重复 snapshot。

### Failure

29. missing input source；
30. invalid enabled index list；
31. inconsistent temporal interval；
32. concurrent selector calculation 只形成一致 snapshot。

## 21. Open Decisions / Deferred

1. `OPEN DECISION`：Provider query projection 如何把多 listing Company 映射为 CIK、exchange +
   ticker、provider symbol 或 bulk request。属于 4.2F。
2. `OPEN DECISION`：scheduled command 在美东午夜附近如何确定 `as_of`。selector 本身只接受
   显式 date，调用方时钟策略必须在 4.2D-1 integration 中固定并测试。
3. `DEFERRED`：Stage 5 将 active WatchlistItem 并入 universe 时，如何版本化 mixed-source
   selector，且不破坏 4.2 caller contract。
4. `DEFERRED`：snapshot/member retention、archive 与 capacity policy，按数据保留决策阶段处理。
5. `DEFERRED`：per-Company manual include/exclude；没有已批准 model 前不得实现。

以上 Open Decisions 不阻塞 provider-independent 4.2D-1 selector implementation。

## 22. Non-Goals

本 ADR 与 4.2D-1 不实现：

- candidate creation；
- company matching；
- reconciliation / dedup / conflict / manual decision；
- EarningsEvent write；
- Provider fetch / live adapter；
- UI / management command redesign；
- Celery、Redis 或 queue；
- watchlist；
- Dockerfile 或 dependency change。

## 23. Gate Decision

```text
PASS — selector contract is implementable
```

理由：

- canonical unit 已确定为 Company；
- as-of temporal semantics 可由 SecurityListing 与 IndexMembership 支撑；
- 非 temporal enabled-index policy 由 explicit input 与 immutable snapshot 处理；
- historical run fact 与 current reconstruction 已分离；
- retry / replay 与 4.2C 已落地 contract 一致；
- Provider mapping 已分層，不阻塞 selector；
- 最小 schema gap 已明确；
- candidate / reconciliation 边界已冻结。

下一阶段：

```text
Stage 4.2D-1 — Monitoring-Pool Selector Implementation
```
