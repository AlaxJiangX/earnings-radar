# ADR-025：通知入队与幂等契约（含 PD-4 落档）

- 状态：已接受
- 日期：2026-10-24
- 决策者：产品负责人（PD-4 已拍板，本文落档）；其余为工程实现决策
- 影响阶段：6.2（入队与去重）、6.4（发送、重试与发送前复查）
- 关闭：ADR-023 §13 PD-4（改为 CLOSED 并指向本文）；
  `docs/development-roadmap.md` §4「移除自选时待发送通知的处理」

## 1. 背景

Stage 6.2 需要把领域变化（日期变化、财报发布、SEC 文件、指数加入/移除/偏移、提前一天）
映射为 Notification 入队，做到重跑与并发下不重复、抑制有原因记录、且与领域变更处于
可靠事务边界。ADR-023 §13 PD-4（移除自选时已入队未发送通知的处理）由产品负责人于
2026-10-24 拍板为方案 A「发送前复查」，本文固化其语义。

## 2. D1：事务边界 = 同库同事务（transactional outbox）

通知与领域变更同在一个 PostgreSQL 事务：`dispatch_*` 编排函数在调用方事务内执行
（内层 `atomic` 成为 savepoint），当前状态、DataChange 与 Notification 一起提交或
回滚。Notification 表本身就是 outbox；6.4 的发送命令是 relay。

拒绝的备选：提交后再发队列（dual-write，提交成功而入队丢失不可恢复）；独立
outbox 进程 + CDC（单库模块化单体下多余基建）；消息代理/Celery（违反技术基线）。

## 3. D2：幂等键 = 稳定业务身份的确定性哈希

```text
sha256("v1" | user_id | event_type | channel | related_object_type
       | related_object_id | event_version | lead_time_minutes)
```

- 稳定业务键而非随机 UUID：重跑、replay 与并发入队收敛到同一行（唯一约束兜底，
  IntegrityError 后按持久化行和解，不依赖先查后写）；
- `event_version` 标识触发「这一次发生」：发布=状态 DataChange id、日期变化=其
  DataChange id、Filing=accession number、指数事件=事实行 id、提前一天=被提醒的
  business date（isoformat）——同一事件的第二次变化是新 occurrence、新键，不算重复；
- `lead_time_minutes` 仅进入 upcoming 的键：同一事件多档 lead 各自成键，互不挤占；
- 规则/偏好**不进键**：改规则不得让同一 occurrence 重新通知。

### 和解语义（同键已存在时）

| 已存状态 | 本次解析 | 结果 |
|---|---|---|
| pending | 任意 | 不动（重跑 no-op；绝不降级） |
| suppressed | 仍抑制 | 更新原因，保持 suppressed |
| suppressed | 不再抑制 | **翻回 pending**（PD-4「移除后重新加回且提醒日未过可重新入队」的实现） |
| processing/sent/failed/cancelled | 任意 | 不动（6.4 投递状态与管理员修正专属） |

抑制在入队时点判定并记录（`suppressed_reason`，ADR-024 三种原因码），但**不是终审**：
发送前复查（PD-4/D4）才是最终权威。

## 4. D3：PD-4 语义与契约（产品负责人 2026-10-24 拍板，方案 A）

- 移除自选的**瞬间不做任何取消**；`WatchlistItem.alerts_enabled=False` 或移除条目
  只影响其后的解析（6.1 策略：无 active 条目即无公司门）；
- **发送前复查**：6.4 发送命令领取 pending 通知后、调用投递前，重新校验「该公司
  是否仍在该用户的有效自选中（就 watchlist 相关事件而言）」，不在则不发送并记录
  抑制原因。复查执行者是 6.4 的发送命令——本 ADR 固化契约，6.2 只保证状态模型
  （`suppressed` + `suppressed_reason`）为它留好位置；
- 移除后又重新加回、且提醒日未过：同一 occurrence 的 suppressed 行在重新 dispatch
  时按 D2 翻回 pending（同键单行，不产生第二条）；
- pending 行不会被入队侧的后续抑制降级（D2 表），避免「先入队、后关开关」与
  「先关开关、后入队」两种顺序产生不同结果——顺序差异统一交给发送前复查收口。

## 5. D4：事件映射与优先级/策略来源

| 事件 | 触发事实（related object / event_version） | 优先级 | 备注 |
|---|---|---|---|
| date_changed | EarningsDateChange / 其 DataChange id | 6.1 矩阵 | 仅 `value_change` 派发；refinement/regression 是否通知仍开放（data-model §14 #1），启用属策略变更 |
| earnings_released | EarningsEvent / 状态 DataChange id | 6.1 矩阵 | 修正后再次发布=新 DataChange=新 occurrence |
| filing | Filing / accession number | 6.1 矩阵 | source_url=filing_url |
| index_added / index_removed | IndexChangeLeg / leg id | 矩阵（LARGE→P1，SMALL→P3） | 纯加腿逐腿派发；公司级 facts 并入本事件 ADDED 腿的指数组（公告期未生效成员也能正确分级） |
| index_transferred | IndexChangeEvent / event id | 矩阵（S/A→P0，B→P1，C→P3） | 同日混合加/卸腿只派 transferred，不再逐腿派 add/remove，避免同一底层事件三连响 |
| full_exit | IndexChangeEvent（exits_base_pool）/ event id | 矩阵（S/A→P0；非自选一律 P3） | 纯卸腿且 `monitoring_impact=exits_base_pool` 时追加；公司已出池仍对全体用户派发（P3），这是非自选用户关于该公司的最后一条池内事实 |
| earnings_upcoming | EarningsEvent / business date isoformat | 矩阵 + lead 判定 | 扫描 `enqueue_due_advance_reminders(market_date)`：canonical 且 scheduled、business date（confirmed 优先，否则 estimated；exact 按 ADR-024 美东自然日）距 market_date 恰为某档有效 lead；候选 lead = 全部规则 ∪ preference 默认 ∪ 1440 |

取消（status=cancelled）的 IndexChangeEvent 不派发。候选身份（identity_status=
candidate）的财报事件不进提前一天扫描（晋升前身份未定；放开属策略变更）。

fan-out 对象为全部 active 用户 ×（email, in_app）两渠道：自选用户按 S/A 实时级，
非自选用户按 B/C（PRD §9.5 摘要雷达语义）；公司对某用户已出监控池时该用户跳过
（full_exit 除外，见上表）。抑制决定同样落行为 suppressed 行（含原因），供审计与
6.4 复核；个人规模下行数受事件量约束，可接受。

## 6. 调研借鉴（设计层面，不复制代码，不引入依赖）

- **Transactional outbox**（microservices.io / AWS Prescriptive Guidance）：消息与
  业务变更同事务落库，relay 异步投递——D1 的直接先例；
- **幂等键设计**（Stripe 式 idempotency key 实践 / 消息系统 dedup 讨论）：随机键只
  保证重试安全，稳定业务键才能跨独立重放去重——D2 采用确定性哈希的依据；
- **通知系统 fan-out + 发送前复查**（经典 notification system 设计、New Relic
  muting rules）：入队时 fan-out 到渠道，mute/偏好状态在**发送时刻**复查，因为
  入队与发送之间状态会变——与 PD-4 方案 A 完全一致，也支持 D2 的「pending 不降级」。

## 7. 备选方案与拒绝理由

- **入队时即对 pending 做取消/降级**（移除自选时主动 cancel）：与 PD-4 拍板相反，
  且引入取消风暴与顺序敏感；拒绝；
- **随机 UUID 或仅 related_object 做幂等键**：无法跨重跑去重 / 无法区分同一事件的
  多次发生；拒绝；
- **digest_bucket 现在入键**：摘要桶是 6.4 决策，现在并入会锁死摘要粒度；留待 6.4
  以追加版本号的方式扩展（v2）；
- **独立 outbox 表 + 轮询转发**：单库单进程下只是把一次 insert 变成两次；拒绝。

## 8. 测试要点

- 每类事件至少一条入队断言（7 类全覆盖）；
- 同一 occurrence 连续 dispatch 两次：不新增行（幂等）；并发入队靠唯一约束和解；
- 渠道关闭 / 公司提醒关闭 / 规则关闭：suppressed + 原因码；重新打开后重派发翻回
  pending（PD-4 re-add 链路）；
- 事务原子性：领域变更回滚 → 无通知残留；
- 跨用户隔离：他人的规则/偏好不影响本用户行；
- 时区边界沿用 ADR-024 惯例（美东 natural day 判定 upcoming due）。

## 9. 影响与未决

- 迁移 `notifications/0002` 只新增 `notification` 表，不改既有表；回滚 = 删表；
- 发送、领取、重试、NotificationDeliveryAttempt、摘要与 digest 字段全部属 6.4；
  页面与已读状态属 6.3；本轮不实现发送前复查本身；
- date_changed 的 refinement/regression 通知范围（data-model §14 #1）与候选事件
  是否进入提醒扫描，保持开放，待产品确认后以策略变更启用。
