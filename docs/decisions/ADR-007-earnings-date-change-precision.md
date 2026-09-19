# ADR-007：财报日期变更边界与值精度

- 状态：已接受
- 日期：2026-09-19
- 决策者：产品负责人
- 影响阶段：4.1B、4.1C、4.1D、4.2、6.x

## 背景

EarningsEvent 当前使用单一 `DateTimeField` 保存预计、确认、实际发布和电话会时间。外部来源可能只提供自然日、盘前/盘后信息或具体时刻；把 date-only 转成 UTC midnight 会伪造事实精度。

4.1B 还需要明确：

- EarningsEvent current state 与 EarningsDateChange domain history 的边界；
- precision refinement / regression 是否属于领域历史；
- EarningsDateChange 与 DataChange、AuditRecord、SourceEvidence 的关系；
- candidate promotion 与 provider reconciliation 的职责边界。

## 决策

### 1. Stage boundary

- 4.1B 只负责 EarningsDateChange、四个发布时间字段及 `release_session` 的 current-state mutation、domain history、DataChange、AuditRecord、SourceEvidence 集成、事务和幂等。
- 4.1C 负责 EarningsEvent status lifecycle、状态转换矩阵和取消/重排 contract。
- 4.1D 只负责 candidate promotion、identity completion、promotion collision detection、promotion idempotency 和 identity mutation audit。
- 4.2 负责 Provider replay、external ID、candidate dedup、cross-provider merge、duplicate reconciliation、source conflict 和 source precedence，并负责解决 4.1D 检测到的 promotion collision；具体契约见 ADR-010。
- 4.1D 不承诺通用 merge / split。没有 provider-independent 的具体用例前，合并、拆分和重复核对归 4.2。

本 ADR 不改变 ADR-001 的 canonical identity 规则。ADR-001 中由候选核对触发的 merge / split 操作如果在未来需要，先由 4.2 提供 provider 和重复核对语义，再在明确的实现阶段落地。

### 2. Current-state representation

以下四个字段分别使用三列表示：

```text
estimated_release
confirmed_release
earnings_release
conference_call
```

每组的合法状态只有：

```text
UNKNOWN
*_at = NULL
*_date = NULL
*_precision = unknown

DATE_ONLY
*_at = NULL
*_date = YYYY-MM-DD
*_precision = date_only

EXACT_DATETIME
*_at = timestamp
*_date = NULL
*_precision = exact_datetime
```

`*_precision` 非空，默认 `unknown`。四个字段均允许 date-only，因为预计、确认、实际发布和电话会来源都可能暂时只有自然日。date-only 不得写入 `*_at`，exact datetime 不得同时写入 `*_date`。

`release_session` 非空，默认 `unknown`，取值为 `pre_market`、`after_market`、`during_market` 或 `unknown`。它独立于上述 precision，只描述市场时段。

当前值只能通过 earnings 领域 Service 写入。数据库 CheckConstraint 必须拒绝 precision 与 `*_at` / `*_date` 冲突的状态。

### 3. Precision model

只有以下 precision：

```text
unknown
date_only
exact_datetime
session_only
```

`session_only` 仅用于 EarningsDateChange 中的 `release_session` 值。EarningsEvent 的 `release_session` 使用领域枚举中的 `unknown` 表达未知，不使用 NULL。

所有 exact datetime 必须为时区感知值并以 UTC 保存。date-only 保留 `date`，不能通过时区转换变成 datetime。

### 4. EarningsDateChange semantics

EarningsDateChange 是 append-only 领域历史，记录四个发布时间字段和 `release_session` 的业务事实变化。

canonical 字段名为：

```text
estimated_release
confirmed_release
earnings_release
conference_call
release_session
```

DataChange 和 SourceEvidence 使用上述逻辑字段名；`*_at`、`*_date` 和 `*_precision` 只是 current-state 表示列，不作为不同字段重复记录。

`change_kind` 取值为：

```text
value_change
precision_refinement
precision_regression
```

分类规则：

- 业务日期、两个 exact datetime 的具体时刻，或两个具体 session 发生实际变化：`value_change`。
- same-day date-only 升级为 exact datetime、unknown 升级为 date/exact、unknown session 升级为具体 session：`precision_refinement`。
- same-day exact datetime 降级为 date-only、date/exact 降级为 unknown、具体 session 降级为 unknown：`precision_regression`。
- canonical value 与 precision 均相同：no-op。

所有非 no-op 变化同时创建 DataChange 和 EarningsDateChange。通知是否发送以及与通知策略无关，由通知阶段决定。

### 5. DataChange relationship

```text
EarningsDateChange
    |
    +-- OneToOneField(data_change_id) --> DataChange
```

DataChange：

- `target_type = earnings_event`
- `target_id = EarningsEvent.id`
- `field_name` 为受控字段名
- `old_value/new_value` 是包含 value 和 precision 的 canonical JSON
- 使用现有唯一 `change_key` 作为幂等依据

canonical JSON 只允许：

```text
null
{"kind":"date","value":"2026-10-24","precision":"date_only"}
{"kind":"datetime","value":"2026-10-24T20:30:00Z","precision":"exact_datetime"}
{"kind":"session","value":"after_market","precision":"session_only"}
```

EarningsDateChange 不再保存第二份 `change_key`。其 `data_change_id` 使用唯一约束，并作为领域历史与通用审计账本的 linkage。

### 6. SourceEvidence relationship

现有 audit schema 的真实关系是：

```text
EarningsDateChange
    -> DataChange.source_evidence_id
    -> SourceEvidence
```

`SourceEvidence.target_type` 仍为 `earnings_event`，`target_id` 为 EarningsEvent UUID，`field_name` 为受控字段。领域 Service 通过 `resolve_source_evidence_reference` 重新加载并验证 SourceEvidence、RawDataRecord、SyncRun、DataSource 和 RawDataObservation。

不为 EarningsDateChange 创建独立 SourceEvidence target。

### 7. Transaction and idempotency

当前值更新、DataChange、EarningsDateChange 和 AuditRecord 必须在同一个 `transaction.atomic()` 中完成。EarningsEvent 必须先 `select_for_update()`。

任一写入失败必须回滚 current state 和全部历史。相同输入重放时：

- canonical value 与 precision 相同：no-op，不新增历史；
- DataChange 已存在：复用其 `change_key` 对应的记录；
- DataChange 存在但对应 EarningsDateChange 缺失或不一致：拒绝并报告审计完整性问题，不自动补造历史。

### 8. Timezone and business date

当前产品范围明确只覆盖 US-listed securities。4.1B 使用 `America/New_York` 作为美股市场自然日、session 和 same-day precision 比较基准。

该时区是当前 US-only contract 的一部分，不声明为未来所有市场的通用事实。若未来支持其他主要交易所，必须新增明显 exchange-aware contract 与 ADR，不在 4.1B 预先抽象。

## 结果

- EarningsEvent 对每个发布时间字段只有一种无歧义表示。
- date-only 不会伪装为 UTC midnight。
- precision refinement / regression 不会因通知策略未定而从领域历史消失。
- EarningsDateChange 不复制 status transition semantics。
- DataChange 是通用字段级账本，EarningsDateChange 是产品领域历史。
- Candidate promotion 与 provider reconciliation 的职责不再重叠。

## 实现门

- EarningsEvent precision 状态与 DB CheckConstraint；
- date-only / exact datetime / unknown 的 service mutation；
- `release_session` 非空迁移与 unknown 语义；
- earnings date change service、DataChange、AuditRecord 的单事务测试；
- same-day refinement、regression、value change、no-op 和 replay 测试；
- SourceEvidence target/field 重新加载校验测试；
- EarningsDateChange 只读 Admin 和 append-only 防护；
- 不实现 4.1C、4.1D 或 4.2。

## 仍待确认

- precision refinement / regression 是否通知用户；
- 日期变化通知中的 old/new status 如何组合；

Candidate promotion 的完整规则已由 ADR-009 确定；cross-provider merge、conflict 和
precedence 的 4.2 契约已由 ADR-010 确定；cancellation、reschedule、correction 和
reinstatement contract 已由 ADR-008 确定。
