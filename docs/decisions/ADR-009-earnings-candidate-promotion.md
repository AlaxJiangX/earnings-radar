# ADR-009：EarningsEvent Candidate Promotion

- 状态：已接受
- 日期：2026-09-19
- 决策者：产品负责人
- 影响阶段：4.1D、4.2

## 背景

4.1A 已建立 `EarningsEvent` 的 canonical/candidate 身份模型：正式身份为
`company_id + period_end_date + period_type`，候选事件使用 NULL
`identity_key` / `identity_rule_version`，数据库通过条件唯一约束保证
CANONICAL 身份不重复。

4.1B 和 4.1C 已分别完成日期变化历史和状态生命周期。`candidate -> canonical`
的 promotion service 尚不存在，ADR-001 中“核对服务将候选提升、合并或拆分”
的总括表述与 roadmap 的 4.1D / 4.2 拆分存在歧义：ADR-007 已将 candidate
dedup、cross-provider merge、duplicate reconciliation、source conflict 和
source precedence 归入 4.2。

本 ADR 关闭 4.1D implementation 前的领域边界，正式确定 promotion 语义、
collision 行为、identity completion、idempotency 和 audit contract。
本 ADR 不改变 ADR-001 的 canonical identity 规则。

## 决策

### 1. 4.1D / 4.2 职责边界

4.1D 只负责：

- candidate -> canonical；
- identity completion；
- promotion collision detection，发现歧义时 fail closed；
- promotion idempotency；
- identity mutation audit。

4.1D 不负责：

- candidate dedup / candidate merge / candidate split；
- cross-provider merge、duplicate reconciliation；
- provider external ID、provider replay；
- source conflict、source precedence。

以上全部属于 Stage 4.2。ADR-001 中“提升、合并或拆分”描述的是完整 future
reconciliation 能力；Stage 4.1D 只实现其中的 promotion，不得自行扩大为
merge / split。

### 2. Promotion 定义

Promotion 是在**同一个 `EarningsEvent` row** 上补齐 canonical identity facts，
并将 `identity_status` 从 `candidate` 原子变为 `canonical`。Promotion：

- 保留原 event UUID、`created_at`、`status`、schedule、EarningsDateChange
  历史、status history 和已有 provenance；
- 不创建第二个 canonical event；
- 不 merge、不 split、不删除候选。

Promotion 是 completion，不是 correction。

### 3. Identity completion 允许范围

| Field | Promotion may set | Replace existing different value |
|---|---|---|
| `company` | No | No |
| `period_end_date` | NULL -> confirmed value | No |
| `period_type` | NULL -> canonical value | No |
| `includes_q4` | derived from `period_type` | derived only |
| `fiscal_year` | No | No |
| `fiscal_calendar_type` | No | No |
| `period_length_weeks` | No | No |
| `identity_status` | `candidate -> canonical` | No |
| `identity_key` | derived | No |
| `identity_rule_version` | derived | No |

如果候选已有 `period_end_date` 或 `period_type` 且与 promotion 输入不同，
service 必须 fail closed；已有值不同属于 identity correction /
reconciliation，不允许通过 promotion 顺手纠正。已有值与输入相同时允许继续，
该字段不写 no-op DataChange。

`company` 不得修改。候选的 `company` 与已确认 identity 不一致时 fail closed，
交由 4.2。`fiscal_year`、`fiscal_calendar_type`、`period_length_weeks`
不属于 promotion mutation surface，保持原值。

### 4. Period label 归一化边界

4.1D service 可以接受 domain period label（例如 `Q4`、`FY`、`ANNUAL`、`Q1`、
`H1`、`OTHER`），并调用现有 `normalize_period_type()`；不得接受 provider
payload 或 provider-specific parsing context，也不得在 service 内实现
provider precedence。

`Q4` 必须继续归一为 `FY` + `includes_q4=true`；未识别 label 进入
`InvalidEarningsPromotion`，不得静默映射为 `OTHER`。

### 5. Existing canonical collision

如果 promotion 派生出的 canonical identity 已存在另一个 canonical event：

```text
fail closed
```

4.1D 不得 merge candidate into existing canonical、不得删除候选、不得复制
status / schedule / history、不得把 existing canonical 作为 promotion 成功返回。

失败后：

- candidate 和 existing canonical 都保持不变；
- 不写 DataChange；
- 不写 AuditRecord；
- 返回稳定 domain collision error，可包含 candidate id、existing canonical id
  和 derived identity key，但不得包含 raw provider payload、secrets 或完整证据 JSON。

普通 collision 不写 rejected mutation history；reconciliation workflow 记录
属于未来 4.2。

### 6. Canonical identity correction 边界

4.1D 不提供 `correct_earnings_identity(...)`。已 canonical 事件：

- 重放相同 identity：返回 `changed=False`，不新增历史；
- 输入不同 identity facts：fail closed。

Canonical identity correction、merge/split 和 duplicate resolution 属于未来
reconciliation（4.2）或独立 ADR；promotion API 不能成为 generic identity setter。

### 7. DataChange contract

Promotion 对每个真实变化的 DB 字段写一条 DataChange：

```text
period_end_date
period_type
includes_q4
identity_status
identity_key
identity_rule_version
```

source facts（`period_end_date`、`period_type`）和系统派生的 identity facts
（`includes_q4`、`identity_status`、`identity_key`、`identity_rule_version`）
都属于 identity mutation，必须审计其 current-state 变化。字段没有真实变化时
不写 no-op DataChange。

DataChange `rule_version` 使用独立 operation rule：

```text
EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION = "earnings-candidate-promotion-v1"
```

`identity_rule_version` 字段继续使用 `IDENTITY_RULE_VERSION = "v1"`。
两者职责分离：后者描述 identity key 派生/归一规则，前者描述 promotion
mutation 规则。

### 8. AuditRecord contract

一次成功 promotion 写：

```text
N DataChange
+ 1 operation-level AuditRecord
```

Action 复用现有 `update`，不新增 audit action enum。before/after 只包含
promotion 相关身份字段（`identity_status`、`period_end_date`、`period_type`、
`includes_q4`、`identity_key`、`identity_rule_version`），不复制整个
`EarningsEvent`。

### 9. SourceEvidence contract

Automatic path 至少提供 SourceEvidence 或 SyncRun；manual path 必须提供
actor、reason 和稳定 request_id/origin key。

一次 promotion 使用一个 record-level evidence（现有 resolver 允许
`field_name` 为空）并复用于该 promotion 的所有 DataChange。派生字段
（`identity_status`、`identity_key`、`identity_rule_version`、`includes_q4`）
不要求 provider 分别提供证据。Service 必须通过现有
`resolve_source_evidence_reference` 重新加载并验证 target type/id、field_name、
SyncRun、RawDataRecord、DataSource 和 RawDataObservation 链；wrong target /
field / chain 拒绝。

### 10. EarningsEvent.source_evidence 行为

Promotion 保持 `EarningsEvent.source_evidence` 不变。Promotion provenance 由
`DataChange.source_evidence` / `SyncRun` 和 operation AuditRecord 表达。
该字段当前不是明确的 latest identity evidence pointer；覆盖它会丢失已有
provenance，field-level 来源由 DataChange 承载。

### 11. Idempotency 与 stale caller

- 同一候选成功 promotion 重放：已 canonical 且 company / `period_end_date` /
  `period_type` / `identity_key` / `identity_rule_version` 与派生结果一致时
  返回 `changed=False`，不新增 DataChange / AuditRecord。
- 已 canonical 且身份输入不同：fail closed。
- 同一 request/evidence retry：复用既有 DataChange / AuditRecord，不重复写。
- Caller 对象 stale 时，service 必须锁 DB row 后重读 persisted state，
  内存中的 candidate 状态不参与合法性判断。

### 12. Concurrency 与 IntegrityError 分类

- 同一候选并发同 promotion：一个真实 mutation，第二个基于 locked persisted
  state 返回 no-op。
- 两个候选并发指向同一 canonical identity：一个成功，另一个 collision
  fail closed；不得 merge、delete 或复制历史。
- DB `identity_key` unique 和 canonical business tuple unique 是最后防线。
- Identity write 使用 nested savepoint；捕获 `IntegrityError` 后，只有当错误
  可明确分类为 canonical identity collision（可重查 winning canonical 行）
  时才转换成 domain collision exception。未知 `IntegrityError` 必须 re-raise，
  失败候选完整 rollback。重查 winning canonical 必须发生在 nested savepoint
  rollback 之后的合法 transaction state，不得在 broken transaction 内直接 query。

### 13. Status / schedule 正交

Promotion 与 4.1B / 4.1C 正交。任何 status（`scheduled_estimated`、
`scheduled_confirmed`、`released`、`cancelled`）都可以 promotion，只要 identity
contract 满足。Promotion 不修改 status、release 字段、precision、
`release_session`，不创建 EarningsDateChange，也不改写已有 date/status history。

### 14. Mutation governance 与 candidate creation

4.1D 继续采用 service-only governance：不新增 `EarningsEvent.save()` /
`QuerySet.update()` 全局 identity guard，依赖 domain service、只读 Admin 和
DB constraints。Direct ORM mutation 仍可绕过审计，这是已知的 deferred
governance risk，不在本 Stage 解决；ORM-level hardening 作为独立任务，
不混入 promotion。

4.1D 不提供 `create_candidate`。Candidate creation、provider external ID 和
provider ingestion 属于 4.2；4.1D tests 使用 fixture / factory 创建候选。

### 15. Migration expectation

4.1D implementation 预期不需要 migration：identity 字段、canonical
completeness constraint、identity_key unique、canonical business tuple unique
和 audit infrastructure 均已存在。任何 promotion history table、merge pointer、
collision table 或 provider external ID 均不属于 4.1D。

## 结果

- 4.1D 的 promotion 语义、collision 行为和 audit contract 不再阻塞。
- Promotion 固定为同 row、同 UUID 的 candidate -> canonical completion。
- Existing canonical collision fail closed，不会被实现成自动 merge。
- Canonical identity correction 与 promotion 分离。
- Promotion 不改变 status / schedule / date history。
- 无新表、无新 action enum、预期无 migration。
- 4.2 保留 candidate dedup、merge/split、provider reconciliation 和
  source precedence。

## 实现门

- 同 row in-place promotion，UUID / status / schedule / history 不变；
- completion-not-correction：已有不同 `period_end_date` / `period_type` 拒绝；
- `company`、`fiscal_year`、`fiscal_calendar_type`、`period_length_weeks` 不变；
- domain period label 通过 `normalize_period_type()` 归一，Q4 -> FY + includes_q4；
- existing canonical collision fail closed，无 merge / delete / rejected history；
- DataChange 覆盖所有真实变化的 identity 字段；
- 使用 `earnings-candidate-promotion-v1` 作为 DataChange rule version，
  `identity_rule_version` 保持 `v1`；
- 一个 operation-level AuditRecord，action `update`；
- SourceEvidence 通过 resolver 验证完整来源链；
- `EarningsEvent.source_evidence` 保持不变；
- same-state / same-request replay 无重复历史；
- stale caller 使用 locked persisted state；
- 并发 promotion 由行锁和 DB unique constraints 保证正确；
- `select_for_update()`、`transaction.atomic()`、savepoint 和
  IntegrityError 分类；
- 不实现 candidate dedup / merge / split / provider reconciliation；
- 无 migration。

## Deferred

- Candidate dedup、cross-provider merge、split、duplicate reconciliation、
  provider external ID 和 source precedence：4.2。
- Canonical identity correction：未来 reconciliation 或独立 ADR。
- ORM-level identity mutation hardening：独立任务。
- Provider-driven candidate ingestion：4.2。
