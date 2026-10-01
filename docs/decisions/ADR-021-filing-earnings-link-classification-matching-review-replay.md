# ADR-021：Filing ↔ Earnings Link、Release Classification、Matching、Review 与 Replay Contract

- 状态：已接受（4.5A Contract Documentation；contract frozen）
- 日期：2026-10-01
- 决策者：产品负责人（Contract Gate 结论：PARTIAL PASS；4.5A 不受 4.5B live blocker 影响）
- 影响阶段：4.5A Filing ↔ Earnings Link & Classification
- 评审基线：`origin/main`，commit `647956e3e5c38c7881caed6ebeb63d06f34a4fdf`
- 范围：只落盘 4.5A 契约。本 ADR 不实现 model、migration、service、selector、UI 或 Provider。

> 状态说明：4.5A = **CONTRACT FROZEN / READY FOR IMPLEMENTATION**。本 ADR 是
> Filing ↔ Earnings 关联、release classification、matching、review 与 replay 的权威决策；
> `docs/data-model.md` 只表达 schema，`docs/architecture.md` 只表达 ownership 与数据流，
> `docs/development-roadmap.md` 只记录阶段状态与验收标准。IR 官方确认的 4.5B 契约见
> ADR-022；4.5B 的 live Provider 仍为 blocked，但不阻塞 4.5A。

## 1. 背景与边界

Stage 4.4 已实现 SEC Filing / FilingDocument 的 metadata 同步，保存 accession number、
form type、accepted_at、period_of_report、primary document、文档目录和 SourceEvidence。
4.4 不下载、不保存 filing body，也不创建 FilingEarningsLink。

Stage 4.5 原先把 Filing 关联与 IR 确认放在同一阶段。Contract Gate 已确认两者可以拆分：

- 4.5A 只需要 SEC metadata，可以在 4.5B live IR Provider 未获批时独立实现；
- 4.5B 依赖公司 IR 官方来源，逐公司、逐来源许可未批准前只能 fixture-first；
- 两者都不能改变 EarningsEvent.status；Filing 只表达监管文件事实，不能推进或倒退财报
  生命周期。

本 ADR 冻结 4.5A 的以下契约：

1. FilingEarningsLink 与 FilingEarningsDecision 的身份、所有权和 schema 形状；
2. periodic matching 与 release matching 的确定性规则；
3. metadata-only release classification；
4. append-only decision、manual review authority 与 replay；
5. selector-derived `has_release_filing` / `has_periodic_filing`；
6. 预期 migration、测试门和非目标。

## 2. Ownership 与依赖方向

`RATIFIED DECISION`

```text
filings app
  owns Filing, FilingDocument, SEC provider parsing and SEC sync
  MUST NOT import earnings or own FilingEarningsLink / FilingEarningsDecision

earnings app
  owns FilingEarningsLink, FilingEarningsDecision,
  matching, release classification, review, replay and the derived filing selectors
  MAY read filings.Filing / filings.FilingDocument through stable model references
```

理由：

- Filing 是 SEC 监管事实，earnings 关联是财报领域语义；
- 现有依赖方向允许 `earnings -> filings`，不允许 `filings -> earnings`；
- 把 link 或 classification 写入 `filings` 会产生循环依赖，并把 SEC Provider 边界与财报
  生命周期语义混在一起；
- `docs/architecture.md` 中把“财报关联”列在 `filings` app 的旧表述必须按本 ADR 修正。

跨模块读取只使用稳定模型引用和公开 selector/service。4.5A 不得通过私有 helper、
monkey patch 或重复解析 SEC raw 来绕过 `filings` 的公开边界。

## 3. Identity 与 relation type

`RATIFIED DECISION`

FilingEarningsLink 的业务身份是：

```text
filing_id
+ earnings_event_id
+ relation_type
```

数据库必须对该三元组设置复合 unique constraint。

`relation_type` 取值为：

```text
RELEASE_FILING
PERIODIC_FILING
OTHER
```

规则：

- `RELEASE_FILING` 表示该 Filing 承载或与本次财报发布材料相关；
- `PERIODIC_FILING` 表示该 Filing 是对应财报期间的定期报告；
- `OTHER` 保留 enum 兼容性，但 4.5A MUST NOT 自动创建、MUST NOT 提供手工创建入口、
  MUST NOT 参与 selector。生产写入只能通过受控 service；直接 ORM 写入不受支持。

同一 Filing 可以关联不同 EarningsEvent，也可以分别拥有 RELEASE_FILING 与
PERIODIC_FILING 关系；每个 (Filing, EarningsEvent, relation_type) 最多一条 link。

## 4. Candidate search contract

`RATIFIED DECISION`

Periodic matching 与 release matching 共享以下搜索边界：

- MUST 只在 Filing 所属的同一个 Company 内搜索；
- MUST 只把 `identity_status = canonical` 的 EarningsEvent 作为可链接 target；
- candidate EarningsEvent 只能触发 review decision，MUST NOT 成为 link target；
- MUST NOT 用 ticker、company name、provider symbol 或 CIK 重新匹配 Company；
- MUST NOT 在 matching 过程中重新运行 monitoring-pool selector，也不得读取当前
  monitoring pool 覆盖 Filing 的持久化 Company；
- 搜索必须使用 persisted Filing / EarningsEvent facts，不得信任调用方内存对象。

Candidate search 的结果语义：

- canonical 结果恰好 1 个，且该事件未取消：允许创建 link；
- canonical 结果为 0，但 candidate 结果至少 1 个：写 `review_required` decision，
  不创建 link；
- canonical 结果多于 1 个：写 `review_required` decision，不创建 link；
- 唯一 canonical 结果是 `cancelled`：写 `review_required` decision，不创建 link；
- canonical 结果为 0 且 candidate 结果为 0：写 `no_match` decision，不创建 link。

Candidate 搜索是 fail-closed 的保护，不是 4.2 reconciliation 的替代。4.2 继续负责
candidate dedup、collision 与 promotion；4.5A 只决定某份 Filing 是否可以安全关联到
一个已存在的 canonical event。

## 5. Periodic matching contract

`RATIFIED DECISION`

Periodic matching 只适用于 10-Q、10-K、20-F、40-F。6-K MUST NOT 在 v1 自动创建
PERIODIC_FILING link，也不进入 periodic matching。

### 5.1 10-Q

候选条件：

```text
Filing.form_type = 10-Q
EarningsEvent.company = Filing.company
EarningsEvent.identity_status = canonical
Filing.period_of_report = EarningsEvent.period_end_date
EarningsEvent.period_type in {Q1, Q2, Q3}
```

唯一候选 → `PERIODIC_FILING` link，confidence = `EXACT`。

### 5.2 10-K / 20-F / 40-F

候选条件：

```text
Filing.form_type in {10-K, 20-F, 40-F}
EarningsEvent.company = Filing.company
EarningsEvent.identity_status = canonical
Filing.period_of_report = EarningsEvent.period_end_date
EarningsEvent.period_type = FY
EarningsEvent.includes_q4 = true
```

唯一候选 → `PERIODIC_FILING` link，confidence = `EXACT`。

### 5.3 无候选与多候选

0 candidate（包括 period_of_report 缺失或无法形成精确匹配）：

```text
no_match decision
no link
```

多于 1 candidate：

```text
review_required decision
no link
```

Periodic link 不要求人工确认。`review_status = auto` 是正常状态；人工确认或拒绝只用于
覆盖自动结果或解决冲突。

## 6. Release matching contract

`RATIFIED DECISION`

Release matching 只适用于 8-K 与 6-K。8-K 的 `period_of_report` MUST NOT 被解释为
财报期间，也不得作为 release matching 的 fiscal identity。

### 6.1 Reference fact precedence

对每个 canonical EarningsEvent，按以下顺序选择第一个非 unknown 的发布时间事实：

```text
earnings_release
>
confirmed_release
>
estimated_release
```

reference date `D` 的推导：

- precision = `exact_datetime`：把时区感知时刻转换到 `America/New_York`，取当地自然日；
- precision = `date_only`：直接使用存储的 date；
- precision = `unknown`：跳过该字段，尝试下一个字段。

`release_session` 不参与 v1 matching。

### 6.2 Window

当 reference date 为 `D` 时，release candidate 窗口为：

```text
[D - 1 day, D + 1 day]
```

等价的 ET 时间是：

```text
[D-1 00:00 America/New_York, D+2 00:00 America/New_York)
```

窗口必须按 ET 自然日而不是 naive UTC 日期计算，并覆盖 DST 切换。

### 6.3 Candidate search and outcomes

Filing 的周期事实搜索范围是同一 Company 的 canonical EarningsEvents，其 reference date
落在窗口内。搜索结果：

- 没有 reference fact：`no_match` decision，reason = `release_fact_missing`，不创建 link；
- 0 candidate：`no_match` decision，不创建 link；
- 多于 1 candidate：`review_required` decision，不创建 link；
- 恰好 1 candidate：允许创建 `RELEASE_FILING` link；
- 唯一 canonical candidate 已取消：`review_required` decision，不创建 link。

8-K 与 6-K 的 classification 规则不同：

- 8-K 按 §7 的 metadata-only decision table 得到 YES / NO / REVIEW_REQUIRED；
- 6-K 可以为唯一窗口匹配创建 `RELEASE_FILING` link，但 classification MUST 永远是
  `REVIEW_REQUIRED`，v1 MUST NOT 自动 YES。

Release link 的 confidence 为 `BOUNDED_WINDOW`。release classification 为
`REVIEW_REQUIRED` 或 `NO` 的 link 可以存在，但 selector 只把 `YES` 计为
`has_release_filing = true`。

## 7. Matching mode 与 confidence

`RATIFIED DECISION`

4.5A 只使用确定性规则：

- 不使用加权 heuristic；
- 不使用数值 pseudo-confidence；
- 不使用 release date proximity 作为 identity；
- 不使用 fuzzy company name、ticker-only 或 provider symbol fallback。

confidence enum：

```text
EXACT
BOUNDED_WINDOW
MANUAL
```

映射：

- periodic exact match → `EXACT`；
- release unique window match → `BOUNDED_WINDOW`；
- manual confirmed → `MANUAL`。

matching rule version：

```text
FILING_EARNINGS_MATCH_RULE_VERSION = "filing-earnings-match-v1"
```

## 8. Metadata-only release classification

`RATIFIED DECISION`

v1 classification 只使用 SEC metadata：

- MUST NOT 下载 filing body；
- MUST NOT 解析 transient body；
- MUST NOT 持久化 excerpt、body hash 或正文；
- Stage 4.4 的 raw/body boundary 保持不变；
- classification 不得通过重新请求 SEC、解析 EX-99 内容或猜测文档语义来补足证据。

### 8.1 Evidence hierarchy

按优先级使用：

1. `Filing.form_type`
2. `Filing.reported_items`
3. `FilingDocument.document_type`
4. `period_of_report` / `accepted_at` / `primary_document` / URL / SourceEvidence，仅用于
   matching 和 provenance
5. `FilingDocument.description`，v1 不可用，因为当前 Stage 4.4 source 保持为空
6. filename 不是语义证据

### 8.2 Filing.reported_items

`reported_items` 来源于 SEC submissions `filings.recent.items`：

- 保存为 canonical comma-separated SEC item codes；
- 每个 code 形如 `n.nn`；
- canonical form 必须 trim、去空、去重、按 item 顺序升序排列，并用逗号连接；
- 空字符串表示 source 未提供、为空或不可解析；
- parser version 升级为 `sec-filings-v2`；
- 单个 malformed `items` 值不得使整份 submissions payload 失败；该 Filing 保存空字符串，
  由 classification 进入 `REVIEW_REQUIRED`。

### 8.3 Supported exhibit allowlist

只允许：

```text
EX-99.1
EX-99
```

`FilingDocument.document_type` 比较前必须 trim + uppercase。`EX-99.x` 中除
`EX-99.1` 以外的值都视为 unsupported exhibit。

### 8.4 Decision table

按以下优先级执行，先命中者生效：

8-K：

```text
1. reported_items 缺失、空或不可解析
   -> REVIEW_REQUIRED / ITEMS_METADATA_MISSING

2. reported_items 包含 2.02
   a. 至少一个 allowlisted exhibit
      -> YES / ITEM_202_WITH_EARNINGS_EXHIBIT
   b. 没有 allowlisted exhibit
      -> REVIEW_REQUIRED / ITEM_202_WITHOUT_SUPPORTED_EXHIBIT

3. reported_items 不包含 2.02
   a. 只有 unsupported EX-99.x
      -> REVIEW_REQUIRED / UNSUPPORTED_EXHIBIT_ONLY
   b. 其他情况
      -> NO / NO_ITEM_202
```

`NO / NO_ITEM_202` 是 positive evidence of non-release。它不表示 Filing 与财报事件无关，
只表示该 8-K 不承载本次 release 材料。

6-K：

```text
REVIEW_REQUIRED / SIX_K_REQUIRES_REVIEW
```

10-Q / 10-K / 20-F / 40-F：

```text
release classification = NULL
not applicable
```

### 8.5 Selector boundary

`REVIEW_REQUIRED` MUST NOT imply `has_release_filing = true`。只有 YES 可以推导 true。

classification rule version：

```text
FILING_RELEASE_CLASSIFICATION_RULE_VERSION = "filing-release-classification-v1"
```

## 9. Review、authority 与 replay

`RATIFIED DECISION`

### 9.1 Current projection 与 append-only history

FilingEarningsLink：

- 是 current projection；
- 只能通过公开 service 修改；
- 不提供通用 update/delete 入口。

FilingEarningsDecision：

- 是 append-only history；
- 使用 `AppendOnlyAuditModel` 与 `AppendOnlyQuerySet`；
- MUST NOT update/delete；
- 通过 `supersedes` self-FK 形成链，而不是就地改写。

### 9.2 Automatic decision

自动 decision：

- MUST 引用 SyncRun；
- decision 与 projection update MUST 在同一个 short transaction 内完成；
- 使用 deterministic `decision_key` 幂等；
- MUST NOT 覆盖最新有效 manual leaf；
- 只使用 persisted facts，MUST NOT 依赖 wall clock 或调用方内存状态。

### 9.3 Manual confirmed

manual confirmed 必须提供：

```text
actor_user
reason
request_id
AuditRecord
reviewed_at
```

Release relation：

```text
review_status = confirmed
release_filing_classification = YES
```

Periodic relation：

```text
review_status = confirmed
```

manual action MUST 在同一个 short transaction 内追加 decision（记录 `decided_at`）、更新
link（`reviewed_by` / `reviewed_at` / `review_reason`）并写 AuditRecord；任一步失败必须
全部回滚。

### 9.4 Manual rejected

manual rejected 必须：

- 追加 manual decision；
- 把 link 的 `review_status` 置为 `rejected`；
- 如果 link 已存在，保留 link，selector 排除 rejected link；
- 如果 link 不存在，rejection 作用域为 `(filing, relation_type)`；
- 在 newer manual decision supersede 之前，automation MUST NOT 重新创建 link。

### 9.5 Authority

```text
manual > automatic
```

最新有效 manual leaf 阻塞 automatic supersession。只有新的 manual decision 可以改变
manual resolution。旧 decision 不得 update/delete；supersession 通过链表达。

### 9.6 Rule-version upgrade

规则版本升级时：

- 新规则产生新 decision；
- automatic leaf 可以被新 automatic decision supersede；
- projection 通过 service 更新；
- 实际字段变化写 DataChange + AuditRecord；
- 值没有变化时只追加 decision，不写 DataChange；
- manual leaf 不能被 automatic rule upgrade 覆盖。

### 9.7 Decision key

`decision_key` 是 canonical SHA-256，至少覆盖：

```text
filing_id
relation_type
target_event_id nullable
ordered candidate event ids
normalized evidence digest
match_rule_version
classification_rule_version
decision_source
```

MUST NOT 包含：

```text
wall clock
raw filing body
DB insertion order
```

manual decision 的 key 还 MUST 包含 actor、request 和 manual action identity。

### 9.8 Concurrency

- automatic evaluation 按 Filing 串行化；
- manual action 锁定 subject 与 latest valid predecessor；
- DB unique `decision_key` 是最后防线；
- 并发冲突必须 fail closed 并重新加载 winner，不得覆盖旧历史。

## 10. Schema contract

本节只冻结 schema 形状。4.5A implementation 负责创建迁移；本次 Contract Documentation
MUST NOT 创建 model 或 migration。

### 10.1 `filings.Filing` extension

| 字段 | 类型/约束 |
|---|---|
| `reported_items` | `CharField(max_length=255, blank=True, default="")` |

`reported_items` 保存 canonical comma-separated SEC item codes。数据库 check constraint：

```text
reported_items = ""
OR reported_items ~ '^[0-9]{1,2}\.[0-9]{2}(,[0-9]{1,2}\.[0-9]{2})*$'
```

历史 Filing：

- migration 后 `reported_items = ""`；
- MUST NOT 猜测历史值；
- classification 在需要时进入 `REVIEW_REQUIRED`；
- 未来 controlled backfill 只能从 persisted SEC raw 重新解析，并写审计 / DataChange；
- backfill 不得改写 accession identity 或历史来源。

parser version：`sec-filings-v2`。

### 10.2 `earnings.FilingEarningsLink`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `filing_id` | FK `filings.Filing`，PROTECT |
| `earnings_event_id` | FK `earnings.EarningsEvent`，PROTECT |
| `relation_type` | RELEASE_FILING / PERIODIC_FILING / OTHER |
| `release_filing_classification` | YES / NO / REVIEW_REQUIRED nullable |
| `classification_reason` | 非 release 为空；release 必填 |
| `classification_rule_version` | release relation 必填；非 release 为空 |
| `match_rule_version` | 非空 |
| `confidence` | EXACT / BOUNDED_WINDOW / MANUAL，非空 |
| `review_status` | auto / confirmed / rejected |
| `review_reason` | auto 时为空；confirmed/rejected 时非空 |
| `source_evidence_id` | nullable，PROTECT |
| `current_decision_id` | FK `FilingEarningsDecision`，PROTECT，非空 |
| `reviewed_by_id` | nullable，PROTECT |
| `reviewed_at` | nullable timestamptz |
| `created_at`, `updated_at` | UTC |

Unique：

```text
(filing, earnings_event, relation_type)
```

Indexes：

```text
(earnings_event, relation_type, review_status)
(filing, relation_type)
```

DB constraints：

- `relation_type = RELEASE_FILING` iff `release_filing_classification` 非空；
- release relation 必须有非空 `classification_reason` 与 `classification_rule_version`；
- 非 release relation 的 `release_filing_classification` 为 NULL，reason 与 version 为空；
- `review_status = auto` → `reviewed_by` / `reviewed_at` 为空且 `review_reason` 为空；
- `review_status in {confirmed, rejected}` → `reviewed_by`、`reviewed_at`、`review_reason`
  非空；
- `review_status = confirmed` 且 `relation_type = RELEASE_FILING` → release classification
  必须为 `YES`；
- `confidence`、`relation_type`、`match_rule_version` 受 choices/非空约束；
- `current_decision` 必须属于同一 Filing 与 relation_type；service 在写入时校验，
  DB 只能表达 FK 存在性。

### 10.3 `earnings.FilingEarningsDecision`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `filing_id` | FK `filings.Filing`，PROTECT，非空 |
| `relation_type` | RELEASE_FILING / PERIODIC_FILING / OTHER |
| `target_event_id` | FK EarningsEvent nullable，PROTECT |
| `decision_type` | 六值枚举，见下 |
| `status` | open / resolved / rejected |
| `classification` | YES / NO / REVIEW_REQUIRED nullable |
| `confidence` | EXACT / BOUNDED_WINDOW / MANUAL nullable |
| `match_rule_version` | 非空 |
| `classification_rule_version` | nullable；classification 非空时非空 |
| `decision_source` | automatic / manual |
| `match_factors` | JSON |
| `reason` | 非空规则由状态约束决定 |
| `source_raw_data_record_id` | nullable，PROTECT |
| `source_evidence_id` | nullable，PROTECT |
| `actor_user_id` | nullable，PROTECT |
| `sync_run_id` | nullable，PROTECT |
| `request_id` | 字符串 |
| `decided_at` | timestamptz，非空 |
| `supersedes_id` | self-FK nullable，PROTECT |
| `decision_key` | char(64)，unique |
| `created_at` | UTC |

Decision types：

```text
matched_release_filing
matched_periodic_filing
review_required
no_match
manual_confirmed
manual_rejected
```

Status：

```text
open
resolved
rejected
```

DB constraints：

- `matched_release_filing` / `matched_periodic_filing` / `manual_confirmed` → status `resolved`，
  target_event 非空；
- `review_required` → status `open`；
- `no_match` → status `rejected`，target_event 为空；
- `manual_rejected` → status `rejected`，target_event 允许为空；
- automatic source → sync_run 非空，actor_user 为空；
- manual source → actor_user 非空、reason 非空、request_id 非空；
- `matched_periodic_filing` → classification 为空；
- `matched_release_filing` / `manual_confirmed` 在 release relation 下 classification 非空；
  `manual_confirmed` 必须是 YES；
- classification 非空时 `classification_rule_version` 非空；
- `decision_key` 为 64 位小写 SHA-256；
- `supersedes` 不得自引用；
- decision_key unique。

Append-only：

```text
FilingEarningsDecision
  extends AppendOnlyAuditModel
  uses AppendOnlyQuerySet
  no update / delete
```

Indexes：

```text
(filing, relation_type, decided_at)
(target_event, decided_at)
(status, decision_type, decided_at)
```

### 10.4 Audit target extension

现有 restricted target enum 已包含 `FILING_EARNINGS_LINK`。4.5A implementation 需要新增：

```text
FILING_EARNINGS_DECISION = "filing_earnings_decision"
```

该值必须加入 `audit.models.DomainTargetType` 与 `audit.models.AuditRecordTargetType`，
使 SourceEvidence / DataChange / AuditRecord 的受限 target 枚举保持一致。不得引入
GenericForeignKey。

### 10.5 Expected migrations

Contract Documentation 不创建迁移。4.5A implementation 预期迁移：

```text
filings/0002  add Filing.reported_items + canonical item check constraint
earnings/0008 create FilingEarningsLink + FilingEarningsDecision + constraints / indexes
audit/0011    extend restricted target enums with filing_earnings_decision
```

实际 migration 编号以 implementation 时的最新基线为准；任一编号变化必须在实现 PR 中
说明，但不得覆盖已有 ADR 或迁移。

## 11. Selector contract

`RATIFIED DECISION`

### 11.1 Derived state

`has_release_filing` 仅在以下条件同时满足时为 true：

```text
relation_type = RELEASE_FILING
release_filing_classification = YES
review_status != rejected
```

`has_periodic_filing` 在以下条件同时满足时为 true：

```text
relation_type = PERIODIC_FILING
review_status != rejected
```

`REVIEW_REQUIRED` 永远不使 `has_release_filing` 为 true。periodic exact matching 不要求
manual confirmation。

### 11.2 Selector output

Selector 除布尔值外，还必须返回：

```text
Filing.form_type
Filing.accepted_at
Filing.filing_url
release_filing_classification
review_status
classification_reason
match_rule_version
classification_rule_version
current_decision_id
```

稳定排序：

```text
accepted_at, filing_id
```

### 11.3 Page boundary

4.5A implementation MUST NOT 修改 templates 或 pages，除非后续 task 单独授权。
Selector 只提供只读查询契约；页面接线属于后续 UI 阶段。

## 12. Implementation acceptance gates

4.5A implementation 至少覆盖：

- schema migration、unique / check / index 约束；
- 10-Q、10-K、20-F、40-F periodic matching；
- 8-K、6-K release matching 与 ET 窗口 / DST 边界；
- `earnings_release > confirmed_release > estimated_release` 的引用优先级；
- metadata-only classification 的 YES / NO / REVIEW_REQUIRED 和原因；
- reported_items canonicalization、空值、malformed 与历史行；
- EX-99.1 / EX-99 allowlist 与 unsupported EX-99.x；
- candidate-only、0 candidate、多 candidate、cancelled canonical 的 review/no-match 路径；
- manual confirmed / rejected、supersede、authority、AuditRecord 和 reviewed fields；
- decision_key 稳定性、重复运行、并发和 DB unique 最终防线；
- rule-version upgrade 不改写 manual leaf；
- selector-derived `has_release_filing` / `has_periodic_filing` 与 ordering；
- 没有 filing body download、没有正文持久化、没有 EarningsEvent.status mutation；
- 同一输入连续执行两次不新增 link / decision / DataChange / AuditRecord；
- SourceEvidence target、SyncRun、DataSource、RawDataObservation 链重新加载校验。

## 13. Non-goals

4.5A MUST NOT：

- 下载或解析 filing body；
- 持久化 release excerpt / body hash；
- 实现 IR Provider、IR observation 或 IR decision；
- 修改 EarningsEvent.status；
- 创建 `OTHER` link；
- 修改 templates / pages；
- 实现 4.5B live gate；
- 绕过 4.2 reconciliation、ADR-009 promotion 或 audit services。

## 14. PRD divergence

PRD §6.1 仍保留旧的 `FILED` 状态语言，且 PRD §7.3 把 SEC EDGAR 描述为“判断财报是否
发布”。这与已接受的 ADR-003 及本 ADR 不一致：

```text
EarningsEvent.status 不包含 FILED。
SEC Filing 不推进或倒退 EarningsEvent.status。
release filing 可用性由 FilingEarningsLink + release classification 派生。
```

按仓库权威来源规则，ADR 的技术建模决策优先；本次 Contract Documentation 保持 PRD
原文不改写，并把该 divergence 记录在 architecture 与 data-model 的说明中。

## 15. References

- `docs/decisions/ADR-003-release-filing-classification.md`
- `docs/decisions/ADR-008-earnings-status-lifecycle-cancellation.md`
- `docs/decisions/ADR-022-ir-confirmation-authority-cancellation-observation.md`
- `docs/data-model.md` §7
- `docs/architecture.md` §4.6、§5.4
- `docs/data-sources.md` §4.3
- `docs/development-roadmap.md` Stage 4.5A
