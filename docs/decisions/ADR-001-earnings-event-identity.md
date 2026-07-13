# ADR-001：财报事件唯一身份

- 状态：已接受
- 日期：2026-07-13
- 决策者：产品负责人
- 影响阶段：4.1 财报事件领域模型及以后

## 背景

`company + fiscal_year + fiscal_period` 无法稳定识别财报事件：Q4 与 FY 可能重叠，外国发行人可能使用 H1/H2，财年会调整，52/53 周财年也会让标签与实际报告期错位。`period_end_date` 有时在第三方预计数据中暂缺，因此还需区分候选与正式身份。

## 决策

正式 EarningsEvent 的业务身份是：

```text
company_id + period_end_date + period_type
```

其中 `period_type` 取值为 `Q1`、`Q2`、`Q3`、`FY`、`H1`、`H2` 或 `OTHER`。年度财报在系统内部一律规范化为 `FY`，并设置 `includes_q4=true`；上游若提供 `Q4` 年度发布标签，也不得另建一个 Q4 正式事件。

模型同时保存：

- `identity_key`：上述字段规范化后生成的稳定唯一键；
- `identity_rule_version`：生成与核对规则版本；
- `identity_status`：`CANDIDATE` 或 `CANONICAL`；
- `fiscal_year`：来源/展示属性，不参与正式唯一键；
- `includes_q4`：年度财报固定为 true，其他期间默认 false；
- `fiscal_calendar_type`：财务日历类型，例如 `MONTH_BASED`、`WEEK_BASED_52_53` 或 `OTHER`；
- `period_length_weeks`：周制财年的实际周数，通常为 52 或 53，非周制可为空。

CANONICAL 事件必须具有非空 `period_end_date`、`period_type`、`identity_key` 和 `identity_rule_version`，数据库对非空 identity_key 设置唯一约束。

当 period_end_date 未知时，可以建立 CANDIDATE 事件，但必须使用 Provider 外部事件 ID、来源和抓取范围进行候选去重。不得用 `company + fiscal_year + period_type` 作为永久唯一键。日期确定后，核对服务将候选提升、合并或拆分为正式事件，并保留旧标识、来源和 DataChange。

## 结果

- 上游 Q4 年度标签统一归并到 `FY + includes_q4=true`，避免同一年度发布形成 Q4/FY 双记录；
- 52/53 周差异由 `fiscal_calendar_type` 与 `period_length_weeks` 表达，不扩张 `period_type`；
- 财年标签变化不会制造新的正式身份；
- 未知 period_end_date 不会被过早固化为错误事件；
- Provider 重放必须使用同一 identity rule version 得到相同结果；
- 规则升级需要显式迁移、冲突报告和审计，不得静默重写历史。

## 实现门

在实现 EarningsEvent Django Model 前，必须把以下内容落实为迁移约束和测试：

- CANONICAL 字段完整性检查；
- 非空 identity_key 唯一约束；
- 上游 Q4 归一到 FY 且 `includes_q4=true` 的测试；
- H1/H2、52/53 周财年及 period_length_weeks 测试；
- period_end_date 未知候选的幂等测试；
- 候选提升/合并的历史与来源保留测试。

## 仍待确认

- 候选事件跨多个 Provider 的自动合并阈值；
- 取消后重新安排是复用原事件还是创建新候选事件。
