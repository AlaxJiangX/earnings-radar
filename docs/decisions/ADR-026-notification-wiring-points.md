# ADR-026：通知接线点与跨模块定向例外

- 状态：已接受
- 日期：2026-10-24
- 影响阶段：6.2W（通知接线）
- 上游契约：ADR-025（入队语义冻结，本文不改任何通知语义）

## 1. 背景

ADR-025 D1 的同事务 outbox 语义要求 dispatch 在持久化领域事实的事务内被调用；
Stage 6.2 交付了 dispatch 函数但没有任何生产调用方。本文固化接线所需的跨模块
定向例外与全部接线点。方向沿用既有先例（earnings 命令/编排层可只读 watchlists
selector）。

## 2. 决策：定向例外

`earnings`、`filings`、`indexes` 的**同步编排/命令层**允许在同一事务内调用
`notifications.services` 的公开 dispatch 函数（只允许
`dispatch_earnings_date_change` / `dispatch_earnings_released` /
`dispatch_filing` / `dispatch_index_change_event` /
`enqueue_due_advance_reminders`，不允许触碰 enqueue 低层与模型写入）。
`watchlists` 不得 import notifications（既有边界不变）。import 一律为接线函数内
的懒导入，避免 app 初始化顺序耦合。

拒绝轮询扫描器替代方案：与 ADR-025 拒绝独立 outbox 轮询的理由相同（单库单进程
下多一跳、丢失同事务原子性）。

## 3. 接线点清单

| # | 领域事实写入点 | 触发条件 | 派发调用 | as_of |
|---|---|---|---|---|
| 1 | `filings.services.record_filing`（外层 atomic） | `filing_created=True` | `dispatch_filing(filing.pk)` | `accepted_at` 的美东自然日（`market_date_from_utc`，可重放稳定） |
| 2 | `earnings.services.ir_confirmation._apply_planned_fields`（两个入口 `evaluate_investor_relations_observation` / `resolve_investor_relations_observation_manually` 的外层 atomic 均经此） | 每次 `update_earnings_schedule` 返回后，逐行 | `change_kind=value_change` 的行 → `dispatch_earnings_date_change`；precision refinement/regression 不派发（ADR-025 §5） | observation `source_observed_at`（缺失回退 `created_at`/`business_date()`）的美东自然日 |
| 3 | 同上 | `mark_earnings_released` 成功返回后 | `dispatch_earnings_released(event.pk, status DataChange pk)` | 同上 |
| 4 | `indexes.services.classify_index_change_event`（本阶段为该函数补齐 `atomic` 包裹，与其写入职责对齐；此前无事务边界） | 每次分类完成（含幂等 no-op 路径） | `dispatch_index_change_event(event.pk)`（6.2 映射自行处理 added/removed/transferred/full_exit） | 分类时刻的 market date（`business_date` 锚定，`market_date_from_utc(timezone.now())`） |
| 5 | `notifications.management.commands.enqueue_advance_reminders` | Cron 驱动（注册留生产部署阶段） | `enqueue_due_advance_reminders(market_date=business_date())`；`--as-of` 供重放；`--dry-run` 全程回滚 | 命令参数或当前 market date |

接线语义说明：

- **幂等归属通知键**：接线点无条件调用 dispatch（接线层不做去重判断），同一
  occurrence 重跑/并发由 6.2 的唯一键与和解逻辑收敛；classify 的幂等 no-op 路径
  同样派发，重跑不产生重复行；
- **候选事件不通知**：`alpha_vantage_canonical` / `candidate_matching` 的
  `update_earnings_schedule` 调用作用于候选事件（ADR-025 §5 候选不通知），故不在
  其内部接线；生产上作用于 canonical 事件的日期变化与发布路径当前只有 IR
  confirmation（4.5B）；未来若出现新的 canonical schedule 写入编排，须按本 ADR
  增补接线点；
- **指数事件演进**：同日多腿经多次 `record_index_change_leg` 逐步到达时，事件
  分类随腿集演进（added-only → transferred 等），每次 classify 派发当前事实集；
  新 occurrence（键含事实行 id）入队、旧 occurrence 不撤销——与 ADR-025 §5
  映射表一致，不作合并；
- **事务边界**：dispatch 在调用方事务内执行（内层 atomic 成为 savepoint）；
  `classify_index_change_event` 此前无 atomic，本阶段补齐包裹属于接线必需的最小
  前置（写入函数获得与其他写服务一致的原子性，无调用方行为变化）。

## 4. 测试要点

- 每类接线至少一条「同步完成 → 通知入队」集成测试（SEC 经 `record_filing`、IR
  经 `evaluate_investor_relations_observation`、指数经 `record_index_change_leg` +
  `classify_index_change_event`）；
- 同一同步重跑（同 accession / 同 observation / 同腿）不产生重复通知；
- 领域回滚（外层事务抛错）无 Filing/通知残留；
- 扫描命令：到期入队、未到期跳过、重跑幂等、`--dry-run` 零写入。

## 5. 影响

- 无迁移、无模型/服务签名变化（classify 的 atomic 包裹为新增前置，不改签名）；
- `architecture.md` 模块表 earnings/filings/indexes 三行与 §5.5 已同步；
- 6.3 起的页面/已读、6.4 的发送与发送前复查不受影响。
