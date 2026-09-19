# ADR-008：EarningsEvent 状态生命周期与取消语义

- 状态：已接受
- 日期：2026-09-19
- 决策者：产品负责人
- 影响阶段：4.1C、4.1D、4.2、6.x

## 背景

EarningsEvent 已有 `SCHEDULED_ESTIMATED`、`SCHEDULED_CONFIRMED`、`RELEASED` 和 `CANCELLED` 状态、4.1B 的日期精度与日期变化历史，以及 DataChange、AuditRecord、SourceEvidence 基础设施，但尚无生命周期服务。

ADR-003 已确定 SEC Filing 不自动推进或倒退 EarningsEvent 状态。ADR-001 和 data-model 将“取消后重新安排是复用原事件还是创建新候选事件”列为待确认问题。

本 ADR 关闭该问题，并正式确定 4.1C 的状态转换、取消、修正、恢复和审计 contract。

## 决策

### 1. Status 领域含义

`EarningsEvent.status` 只表达该 logical earnings event 当前所处的生命周期阶段：

```text
scheduled_estimated
scheduled_confirmed
released
cancelled
```

它不表达：

- SEC Filing 是否存在；
- conference call 是否单独取消；
- 某一次发布时间是否变化；
- 某个 Provider 当前是否返回该记录。

### 2. `cancelled` 正式定义

`cancelled` 表示：

> 这个 EarningsEvent 本身已经被明确撤销、证实不会按当前事件身份发生，或此前记录被证明是一个不成立的 earnings event。

`cancelled` 不表示：

- conference call 被取消；
- 发布时间调整；
- 发布日期推迟；
- Provider 改变预计日期；
- schedule precision 下降；
- Provider 某一轮没有返回记录。

电话会取消、日期变化和 precision regression 分别属于独立未来需求或 4.1B 日期变更。当前不新增 `conference_call_status`。

### 3. Cancellation 证据边界

自动 cancellation 必须基于 affirmative evidence，例如：

- 公司 IR 明确取消；
- authoritative source 明确撤销；
- 可靠 reconciliation 明确证明 event 不成立。

Provider absence、缺失记录、空响应或单次未返回不得触发 `cancelled`。

真实来源优先级由 4.2 决定，但 4.1C 只接受明确的 cancellation evidence。

### 4. Normal Transition Matrix

```text
scheduled_estimated -> scheduled_confirmed
scheduled_estimated -> released
scheduled_estimated -> cancelled

scheduled_confirmed -> released
scheduled_confirmed -> cancelled
```

`scheduled_estimated -> released` 允许直接发生。系统不得为了补齐流程伪造 `scheduled_confirmed` 阶段。

Same status 是 idempotent no-op。

Normal transition 可以由自动来源或人工操作发起：

- 自动路径至少需要 SourceEvidence 或 SyncRun；
- 人工 normal transition 需要 actor、reason、request/origin identity；
- AuditRecord 使用现有 `update` action，不新增 action enum。

### 5. Released 与 Cancelled Terminal Semantics

Normal lifecycle 下：

- `released` 是 terminal；
- `cancelled` 是 terminal。

Terminal 不等于数据库永久禁止修正。反向变化只能通过 audited correction 或 reinstatement path，不允许普通 transition API 执行。

### 6. Correction-only Transitions

以下变化只允许通过 correction path：

```text
scheduled_confirmed -> scheduled_estimated
released -> scheduled_confirmed
released -> scheduled_estimated
released -> cancelled
```

Correction 要求：

- actor；
- reason；
- stable request/origin identity；
- 可选 SourceEvidence；
- DataChange；
- AuditRecord。

Correction 的前提是原 status 被证明错误，并且 event identity 本身仍然正确。

### 7. Erroneous Cancellation

如果取消记录本身就是错误事实：

```text
cancelled -> scheduled_estimated
cancelled -> scheduled_confirmed
cancelled -> released
```

使用 `correct_earnings_status(...)`，保留原 cancellation 历史，并写 `manual_correction` AuditRecord。

### 8. Reinstatement

如果 cancellation 当时是真实事实，但同一 logical earnings event 后来重新安排，则使用显式：

```text
reinstate_earnings_event(...)
```

允许目标状态：

```text
scheduled_estimated
scheduled_confirmed
released
```

Reinstatement：

- 复用同一个 EarningsEvent；
- 保留同一个 canonical identity；
- 不删除原 cancellation DataChange/AuditRecord；
- 不伪造中间状态；
- 如存在新 schedule fact，由 4.1B service 写入；
- 只恢复 status 而 schedule unknown 也允许；
- 自动路径要求 SourceEvidence，人工路径要求 actor、reason、request identity。

Reinstatement 不是 correction。人工 reinstatement 使用现有 AuditRecord action `update` 加 reason，不新增 audit action enum。

### 9. Same-Identity Reappearance

对于同一个 canonical earnings identity：

```text
company_id + period_end_date + period_type
```

真实取消后重新安排必须恢复原 EarningsEvent，而不是创建第二个 canonical event。

理由：

- EarningsEvent identity 表达公司某财务期间的 logical event，不表达一次 schedule attempt；
- schedule date/time 已由 4.1B 作为可变化事实建模；
- canonical identity uniqueness 不允许第二个 canonical event；
- 恢复原 event 能保留完整 cancellation/reinstatement history。

### 10. Identity Uncertainty

如果重新出现的安排不能证明属于同一 identity，或涉及：

- company 错误；
- fiscal period 错误；
- duplicate provider event；
- candidate/canonical collision；
- cross-provider duplicate；

4.1C 必须 fail closed，不执行 reinstatement，并交给：

- 4.1D candidate promotion / identity completion；
- 4.2 provider reconciliation。

4.1C 不负责 identity merge、split、promotion 或 provider precedence。

### 11. Status History

不新增 `EarningsStatusChange`。

Status history 使用：

```text
DataChange(field_name="status")
+
AuditRecord
```

DataChange：

- target 为 EarningsEvent；
- old/new value 为受控 status string；
- source evidence 可选；
- rule version 为 `earnings-status-lifecycle-v1`。

AuditRecord before/after 只记录 status，不复制整条 EarningsEvent。

### 12. SourceEvidence

机器驱动的 normal transition、cancellation 或 reinstatement 应支持：

```text
SourceEvidence.target_type = earnings_event
SourceEvidence.field_name = status
```

领域 Service 必须通过现有 resolver 重新加载和验证完整来源链。

Manual correction/reinstatement 可以没有 SourceEvidence，但必须有 actor、reason 和 stable request/origin identity。

### 13. 4.1B / 4.1C Orchestration

4.1B 继续只负责 schedule/date/session。

4.1C 继续只负责 status。

同一 observation 同时包含 schedule 和 status fact 时：

```text
higher-level orchestration
  -> outer transaction
  -> 4.1B update_earnings_schedule
  -> 4.1C transition / reinstate
```

Schedule 先写入，status 后写入。两步任一步失败，outer transaction 全部 rollback。

4.1C 不复制 date normalization、precision 或 EarningsDateChange logic。

### 14. Invalid Transition Behavior

普通 transition API 遇到 correction-only 或 reinstatement transition 时：

- fail closed；
- raise domain exception；
- 不修改 current state；
- 不写 DataChange；
- 不写 AuditRecord。

### 15. Idempotency 与 Concurrency

Same status replay 返回 `changed=False`，不写历史。

每次 mutation：

- `transaction.atomic()`；
- `select_for_update()` 锁定 EarningsEvent；
- old status 来自 locked DB current state；
- correction/reinstatement retry 使用 stable request/origin identity；
- concurrent conflicting transition 由第二个锁定者基于新 current state 重新判断。

### 16. Rule Version

正式定义：

```text
EARNINGS_STATUS_LIFECYCLE_RULE_VERSION = "earnings-status-lifecycle-v1"
```

该版本：

- 与 `identity_rule_version` 分离；
- 与 `earnings-date-change-v1` 分离；
- 用于 status DataChange 的规则版本。

## 最终 Transition Matrix

| From | To | Classification | Required Path |
|---|---|---|---|
| `scheduled_estimated` | `scheduled_confirmed` | normal | transition service |
| `scheduled_estimated` | `released` | normal | transition service |
| `scheduled_estimated` | `cancelled` | normal | transition service + affirmative evidence |
| `scheduled_confirmed` | `released` | normal | transition service |
| `scheduled_confirmed` | `cancelled` | normal | transition service + affirmative evidence |
| `scheduled_confirmed` | `scheduled_estimated` | correction_only | correction service |
| `released` | `scheduled_confirmed` | correction_only | correction service |
| `released` | `scheduled_estimated` | correction_only | correction service |
| `released` | `cancelled` | correction_only | correction service |
| `cancelled` | `scheduled_estimated` | correction_only 或 reinstatement | erroneous cancellation correction；真实取消后重排 uses reinstatement |
| `cancelled` | `scheduled_confirmed` | correction_only 或 reinstatement | 同上 |
| `cancelled` | `released` | correction_only 或 reinstatement | 同上；可直接进入 released，不伪造中间状态 |
| 任一 status | 相同 status | no_op | no history |

## 结果

- 4.1C cancellation/reschedule contract 不再阻塞。
- 同一 canonical identity 不会因真实取消后重排而创建第二个 canonical event。
- Erroneous cancellation 与 genuine reinstatement 的历史语义可区分。
- Conference call cancellation 不再被误映射为 event cancellation。
- Provider absence 不会被误判为 cancellation。
- Status history 继续使用现有 audit infrastructure，不需要新表。
- 4.1B 与 4.1C 的职责保持分离。

## 实现门

- normal transition matrix；
- same-state no-op；
- direct estimated -> released；
- correction-only transitions fail closed in normal API；
- cancelled reinstatement 复用同一 EarningsEvent；
- absent provider record 不触发 cancelled；
- DataChange status 和 AuditRecord；
- SourceEvidence target/field 校验；
- outer transaction rollback；
- lock-based concurrency 和 stale caller 处理；
- 不实现 4.1D promotion 或 4.2 reconciliation。

## Deferred

- Provider 具体 cancellation evidence precedence：4.2。
- Cancellation 后 identity 无法确认时的 promotion/collision resolution：4.1D/4.2。
- Conference call 独立取消状态：未来产品需求。
- Status change notification 的投递与去重：通知阶段。
