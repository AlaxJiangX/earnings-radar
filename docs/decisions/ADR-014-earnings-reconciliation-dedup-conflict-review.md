# ADR-014：财报 Reconciliation、Dedup、Conflict、Review 与 Manual Authority

- 状态：已接受
- 日期：2026-09-25
- 决策者：产品负责人
- 影响阶段：4.2E，并为 4.2F 提供 stable contract
- 评审基线：`origin/main`，commit `cd9dd53e6b943edaf53d59f3e01342e01cf34e46`

## 1. 背景

Stage 4.2A 已由 ADR-010 冻结 exact-only reconciliation、字段 authority、no destructive
merge、manual override 和 promotion 边界；Stage 4.2B 已实现 append-only
`EarningsReconciliationDecision` foundation；Stage 4.2C 已实现 fixture-first ingestion /
replay；Stage 4.2D 已实现 monitoring-pool selector 与 candidate/company matching。

仍待 4.2E 明确的是：

- candidate 之间如何形成可比较的 reconciliation group；
- exact identity 之外，哪些差异是 compatible evidence、哪些是 conflict；
- 何时可以自动 dedup，何时必须 review_required；
- manual decision 的 authority、supersession 和并发行为；
- 何种条件下可以调用 ADR-009 promotion，何时必须保持 candidate；
- deterministic execution identity 和 audit chain 如何保持可重放。

本 ADR 只冻结 contract，不实现 service、model、migration、Provider、UI 或 notification。

## 2. Scope 与非目标

### 已批准范围

4.2E 实现 fixture-first、provider-neutral 的：

- candidate reconciliation group evaluation；
- exact-only dedup / duplicate mapping；
- conflict classification；
- review_required decision；
- append-only manual decision authority；
- 在满足 ADR-009 条件时 orchestration promotion；
- deterministic decision identity、input revision 和并发幂等。

### 非目标

4.2E MUST NOT：

- 做 fuzzy matching 或基于 release date proximity 的自动合并；
- 建立 Provider-specific trust ranking / precedence；
- 做 destructive merge、删除 loser 或复制 loser 历史；
- 直接改写 canonical identity，或绕过 `promote_earnings_event`；
- 修改 `estimated_release` / `release_session` 以外的第三方 calendar 字段；
- 处理 IR / SEC 高 authority 字段矩阵；
- 实现真实 Provider、同步 command、scheduled runtime wiring；
- 实现 review UI、SLA、通知或 watchlist。

## 3. Reconciliation unit 与 grouping

### 主体单元

4.2E 的 reconciliation subject 是：

```text
EarningsEvent(identity_status=candidate)
```

它必须能追溯到至少一条 `created_candidate` decision 和该 decision 的
`EarningsCalendarObservation` / raw lineage。decision history 仍以 observation 为写入单位，
但 comparison unit 是 candidate EarningsEvent。

### Candidate grouping

两个 candidate 只有在以下事实全部存在且相等时，才进入同一 automatic candidate group：

```text
Company.id
period_end_date
period_type
```

`Company` 相同本身 MUST NOT 被解释为“同一财报事件”。缺 `period_end_date`、缺
`period_type` 或只有 fiscal label 的记录可以进入 review，但 MUST NOT 进入 automatic
dedup group。

同一 group 内 candidate 按 UUID 排序形成 deterministic set。实现 MUST NOT 使用数据库
插入顺序、`created_at`、Provider 返回顺序或 QuerySet 默认顺序作为 business priority。

### Source identity

`provider_key + provider_event_id` 只证明 provider event lineage：

- 同 source + 同 external ID 可以证明是同一 provider event 的 revision；
- 它不自动证明 canonical period identity；
- 当同一 external ID 指向不同 Company / period identity 时，必须写
  `collision` / `review_required`，不得猜选一方。

## 4. Event / period identity

### Definitive identity

自动 dedup / promotion 至少要求：

```text
company A == company B
period_end_date A == period_end_date B
period_type A == period_type B
```

并且：

- `period_end_date`、`period_type` 非空；
- normalized `period_type` 已通过 ADR-001 / ADR-009 的规则；
- fiscal calendar facts 不冲突；
- 4.2 calendar 权限范围内的 known schedule facts 不冲突。

### Fiscal calendar compatibility

- `UNKNOWN` 与任何单一 known value 兼容，known value 优先保留；
- 两个 known `fiscal_calendar_type` 不同 => `review_required`；
- `period_length_weeks` 只在 week-based calendar 上有意义；
- week-based candidate 缺少 52/53 => `review_required`；
- 两个 known `period_length_weeks` 不同 => `review_required`。

### Fiscal year / display facts

`fiscal_year` 不是 canonical identity 的一部分，但它仍是可展示事实：

- unknown vs known：compatible；
- 两个 known 值不同：`review_required`，不得静默选择一个；
- 4.2E MUST NOT 用 fiscal year 反推 identity。

### Status

4.2E MUST NOT 创建、取消或推进 status。若 candidate group 内 status 不同，自动 promotion
必须进入 `review_required`；manual resolution 只能授权既有 status 的 winner，不能伪造状态转换。

## 5. Dedup outcome

内部 outcome 使用受控 namespace
`match_factors["earnings_reconciliation"]["outcome"]`：

```text
DEFINITE_DUPLICATE
NOT_DUPLICATE
REVIEW_REQUIRED
```

### DEFINITE_DUPLICATE

满足全部条件时，candidate 属于同一 logical event：

- exact group key 完全相同；
- Company identity 与 persisted candidate Company 一致；
- period identity 完整且一致；
- fiscal / schedule facts compatible；
- 没有 unresolved manual authority；
- 没有 collision / conflict review。

winner 规则：

1. 已存在同 canonical identity 的事件时，canonical event 是 winner；
2. 否则在 compatible candidate 中按固定 completeness vector 选择更完整的事实：
   known fiscal calendar、known period length、known fiscal year、known estimated release、
   known release session；
3. completeness 相同则选择 UUID 字典序最小者。

这个 winner 规则只在语义可兼容且 identity 完全一致时生效，不表示 Provider 优先级。loser
MUST 保持 `candidate` 和全部历史，只写 `duplicate_of` / mapping decision。

### NOT_DUPLICATE

以下情况不是 duplicate：

- Company 不同；
- `period_end_date` 或 `period_type` 都完整但不同；
- 同一 Company 的不同 fiscal period。

如果仍需留痕，使用 `no_match` / `rejected`，并在 controlled namespace 中记录
`not_duplicate` reason；MUST NOT 创建 target 或 promotion。

### REVIEW_REQUIRED

只要存在下列任一条件，自动 resolution 必须停止：

- group identity 缺失或不完整；
- 两个 known identity facts 冲突；
- source external mapping 冲突；
- fiscal calendar / period length / fiscal year conflict；
- schedule field 的 known value conflict；
- status conflict；
- manual authority 尚未 resolved；
- canonical collision；
- 任何需要 fuzzy 或任意 priority 判断的情况。

使用 `review_required` / `open`，target 为 NULL，直到新的 manual resolved decision
supersede 该 open decision。

## 6. Conflict taxonomy

| Field / fact | Compatible | Conflict / review | Automatic action |
|---|---|---|---|
| Company | exact same persisted Company | different Company | `NOT_DUPLICATE`；external mapping 冲突则 review |
| period_end_date | equal known | different known 或缺值 | review；完整且不同可作为 not duplicate |
| period_type | equal known | different known 或缺值 | review；完整且不同可作为 not duplicate |
| fiscal_calendar_type | unknown vs known；相同 known | 不同 known | review |
| period_length_weeks | unknown vs known；相同 known | 不同 known；week-based 缺 52/53 | review |
| fiscal_year | unknown vs known | 不同 known | review |
| estimated_release | unknown vs known；相同 business date；ADR-007 precision refinement | 不同 date/datetime/session 事实 | review |
| release_session | unknown vs known；相同 known | 不同 known | review |
| status | 相同 status | 不同 status | review；4.2E 不执行 status transition |
| provider_symbol / company_name | evidence-only difference | 若影响 Company identity | review，不自动 fuzzy |
| confirmed / earnings release / conference call | 不由 4.2E 自动写入 | known conflict | review；留给后续 authority |

`UNKNOWN` 是 absent source fact，不等于与 known fact 冲突。不得因为 unknown 与 known
不同而覆盖 known；也不得因为 model default 产生 known fact。

## 7. Automatic resolution 与 review_required

### Automatic

自动 resolution 只能：

- 建立 exact candidate group；
- 选择 canonical / deterministic winner；
- 为 loser 写 append-only `duplicate_of` mapping；
- 在 identity 完整、无 conflict、无 open review 时调用 ADR-009 promotion；
- 通过既有 schedule service 应用 compatible known `estimated_release` /
  `release_session`。

自动 resolution MUST NOT：

- 用 Provider trust ranking、日期接近或 fuzzy 规则选 winner；
- 覆盖已有 manual authority；
- 为一个 conflict 直接写 resolved decision；
- 修改旧 decision、删除旧 candidate 或重写 identity。

### Review_required

任何无法由 exact rules 和 compatible unknown/known rules确定的场景，必须进入
`review_required` / `open`。review 期间：

- candidate / observation / evidence 全部保留；
- 不 promotion；
- 不 merge；
- 不覆盖旧 schedule；
- 可以被新的 manual decision 或未来已批准规则 resolve。

## 8. Manual decision authority

### Allowed manual actions

manual decision 必须是 append-only `EarningsReconciliationDecision`，并至少包含：

- `actor_user`；
- 非空 `reason`；
- 非空 `request_id`；
- deterministic `decision_key`；
- 同一事务 AuditRecord；
- optional `target_event` / `supersedes`；
- `covered_fields` 仅允许 `estimated_release`、`release_session`。

manual action MAY：

- 将 `review_required` 解析为 definite duplicate / not duplicate；
- 选择现有 candidate 或 canonical event 作为 winner；
- 明确选择 `estimated_release` / `release_session` 的受审计值；
- 授权满足 ADR-009 的 promotion；
- 拒绝 duplicate / 保持 separate。

manual action MUST NOT：

- 修改 `company`、`period_end_date`、`period_type`、fiscal identity 或 canonical identity；
- 伪造不存在的 Provider identity；
- 删除、覆盖或搬移旧 decision / candidate / evidence；
- 绕过 schedule service、promotion service 或 AuditRecord。

### Authority and supersession

manual authority 从 append-only decision history 推导，不加可变 `locked` field：

- 某个 observation / candidate / covered field 的最新有效 manual decision 高于后续自动
  decision；
- 自动处理 MUST NOT supersede manual decision；
- 新 manual decision 可以 supersede 旧 decision；
- 旧 decision 原样保留，通过 `supersedes` 链表达；
- 同一 `request_id` 重放必须复用同一 decision；
- 同一 `decision_key` 但不同 persisted facts 必须 fail closed。

manual decision 必须锁定 reconciliation subject，重新读取 latest persisted decision 后再
创建 successor，禁止并发生成两个无共同前驱的有效分支。

## 9. Decision state machine

现有 row-level states 保持：

```text
open / resolved / rejected
```

- `open`：`collision`、`conflict`、`review_required`；
- `resolved`：`created_candidate`、`matched_candidate`、`matched_canonical`、
  `duplicate_of`，必须有 `target_event`；
- `rejected`：`no_match`、`ignored`，不得有 `target_event`。

decision row 是不可变事实。所谓“状态转换”只能通过新 decision + `supersedes` 表达：

1. open review 可以由新的 manual `duplicate_of` / `matched_candidate` /
   `matched_canonical` / `no_match` 解决；
2. resolved / rejected decision 可以被后续 correction 通过新 decision supersede；
3. 旧 row 不得 DELETE / UPDATE；
4. 不允许跨 observation supersede；
5. 不允许 self-supersede；
6. 不允许 resolved target 为空、rejected target 非空或非法 decision type/status 组合。

## 10. Canonical promotion boundary

4.2E 可以使用 ADR-009 `promote_earnings_event`，但只有满足以下全部条件：

- candidate Company 与 group identity 完全一致；
- `period_end_date` 和 normalized `period_type` 已完整；
- 没有 open `review_required` / `collision` / `conflict`；
- 已解决所有 known fiscal / schedule / status conflict；
- source external ID mapping 没有冲突；
- winner 不是已有 canonical 的 conflicting duplicate；
- manual authority 若存在，必须已经 resolved 且没有更新的 superseding manual decision。

规则：

- 已有 canonical identity：candidate MUST 保持 candidate，loser 只写 mapping 指向 canonical；
- 没有 canonical：选出的 winner 通过 ADR-009 在同一 UUID 上 promotion；
- loser MUST 保持 candidate 和历史，不复制、不删除；
- promotion collision MUST fail closed，写 open collision/review decision，不覆盖任何一方；
- review_required 必须阻止自动 promotion；
- manual resolution 可以解除 review，但不得改变 canonical identity facts；
- promotion 后的 canonical row 不得由 4.2E 改写 identity；后续 correction 需要新的 decision
  或未来 ADR。

## 11. Audit、evidence 与可追溯性

每次 reconciliation decision 至少保留：

- subject observation / raw lineage；
- candidate / peer candidate IDs；
- exact group key；
- source external identity；
- rule version 与 input revision；
- conflict codes 与 compatible facts；
- winner / loser target；
- manual actor、reason、request id、decided_at；
- supersedes predecessor；
- resulting canonical target（若 promotion 成功）。

同一事务必须写入对应 AuditRecord。schedule 变化通过既有 schedule service 产生 DataChange /
EarningsDateChange / AuditRecord。loser 的 SourceEvidence、candidate、schedule / status
history 和旧 decision 必须保留并继续可查询。

## 12. Versioning、input revision 与 execution identity

4.2E 使用：

```text
EARNINGS_RECONCILIATION_VERSION = "earnings-reconciliation-v1"
```

`match_factors["earnings_reconciliation"]` 至少保存：

- version；
- reconciliation execution key；
- reconciliation input revision；
- outcome / reason_code；
- candidate group key；
- ordered subject / peer candidate IDs；
- source and period facts；
- conflict codes；
- winner / loser IDs；
- manual authority predecessor ID（如有）。

`reconciliation_input_revision` 是 canonical JSON 的 SHA-256，覆盖：

- EARNINGS_RECONCILIATION_VERSION；
- subject candidate / observation stable IDs；
- candidate group semantic facts；
- ordered peer candidate stable IDs 与 observation IDs；
- source / provider event identity；
- Company and period identity facts；
- compatible / conflicting field facts；
- relevant SourceEvidence stable IDs；
- 当前有效 manual decision IDs（仅作为 predecessor identity）。

MUST exclude：

- wall clock；
- DB insertion order；
- random UUID 的生成瞬间；
- Provider availability；
- current MarketIndex enablement；
- current monitoring status；
- 无关的未参与候选。

`reconciliation_execution_key` 由 version、subject observation / candidate、ordered candidate
group 和 input revision 生成。相同 execution 重复执行必须复用同一 `decision_key`，不得
产生第二个 equivalent decision、mapping 或 promotion。

## 13. Concurrency、idempotency 与 failure

- 每条 reconciliation decision 使用短 transaction；
- 同一 subject / observation 的 decision 写入必须串行化；
- manual decision 必须锁定 subject，再读取 latest valid predecessor；
- 并发相同 request：一个真实 create，其余 reload winner 并验证后复用；
- 并发不同 manual decision：后到者必须 supersede 已提交的最新有效 decision，不得产生
  两个无共同前驱的 leaf；
- promotion collision、source mapping conflict、decision key collision 或 persistence
  invariant failure 必须 fail closed；
- 同一 subject 的 binding decision、必要 schedule authority 和 promotion/audit 必须在一个短
  transaction 内 all-or-nothing；partial persistence 不允许留下 promoted winner 但缺失对应
  decision/audit；
- retry 必须只复用已提交 decision，不重复 candidate/evidence/audit；
- 不把整个 candidate group 包进一个长 transaction。

## 14. Schema decision

现有 schema 足够表达本 ADR：

- `EarningsReconciliationDecision.observation` 保存 subject lineage；
- `decision_type` / `status` 保存 outcome；
- `target_event` 保存 winner / canonical target；
- `covered_fields` 保存 manual schedule authority；
- `match_factors` 的受控 namespace 保存 grouping、revision、conflict 与 winner/loser evidence；
- `actor_user`、`sync_run`、`request_id`、`reason`、`decided_at` 保存 authority/audit context；
- `supersedes` 保存 append-only chain；
- `decision_key` unique 与 service transaction 处理并发幂等。

```text
schema change required = NO
migration expected = NO
```

如果 implementation 发现现有 enum / constraint 无法表达已经批准的 4.2E 语义，必须停止并
重新 ratify，不得顺手新增模型或 migration。

## 15. Runtime boundary

```text
Stage 4.2E requires production runtime wiring = NO
```

4.2E 只实现 provider-neutral reconciliation domain/service 和 fixture-based tests。同步
command、真实 Provider、scheduled ingestion 生命周期和 pre-finalize candidate/reconciliation
integration 属于后续 4.2F / orchestration 工作；本轮不得改变 ingestion lifecycle。

## 16. Gate decision

```text
PASS — 4.2E reconciliation / dedup / conflict / review / manual authority contract is implementable
```

理由：

- exact-only automatic reconciliation 已由 ADR-010 冻结；
- candidate grouping 可复用 Company + period identity；
- duplicate / collision / conflict / review 可复用现有 decision、target、supersedes 和 audit；
- manual authority 可通过 actor/reason/request/covered_fields 表达；
- promotion 只能通过 ADR-009 且可被 unresolved review 阻止；
- 无需 schema change，且不混入 Provider 网络或 runtime orchestration。
