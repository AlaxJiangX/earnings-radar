# ADR-028：数据保留策略与清理任务契约（草案）

- 状态：草案（Proposed）。数字按产品负责人 2026-10-25 拍板落档，合并前由产品
  负责人逐项认可；认可后状态改为「已接受」。
- 日期：2026-10-25
- 决策者：产品负责人（保留数字）；契约化与实现边界由本文按 AGENTS.md §8 推导
- 影响阶段：8.1（契约落档）；清理任务实现属后续小阶段（建议 8.4 前）
- 关闭：`docs/development-roadmap.md` §4「原始/通知/审计数据保留、账号删除与
  watchlist 匿名化 | 8.1 前（ADR-023 §13 PD-6）」的保留数字部分；
  `docs/data-sources.md` §10「原始响应保留期限、长期容量/删除政策」；
  `docs/architecture.md` §4.3 与 §14 的对应待确认项。
  账号删除与 watchlist 匿名化不在本文范围，延至 8.4 前另议（见 §6）。

## 1. 背景

ADR-023 §13 将「账号删除、匿名化和数据保留期限」记为 DEFERRED PD-6，最晚
Stage 8.1。项目规模基线已由产品负责人确认为个人/朋友使用；存储 MVP 只用
PostgreSQL（无对象存储）。MVP 阶段 0–7 已产生多类只增不改的审计/历史记录与
两类可清理数据（原始响应正文、通知投递记录），没有成文保留策略时，磁盘容量
与「历史只增不改」宪法之间的张力无法给出可执行的边界。

## 2. 保留桶划分原则

按数据性质分三个桶，而不是逐表临时定价：

1. **审计与领域历史（永久）**：一切 append-only 的审计链与领域事实。它们是
   「为什么当前值是这样」的唯一证据，AGENTS.md §8 明确不得改写或删除。
2. **投递与操作负载（到期删除）**：通知行与其投递尝试是队列/投递记录，不是
   领域事实；保留 365 天供个人追溯后可物理删除。
3. **大体积原始正文（到期清空）**：原始响应正文是来源证据的「原文」，但个人
   规模下长期保留收益递减；到期清空正文、保留行与全部指纹/哈希/大小证据，
   是「永久保留证据链」与「有限容量」之间的折中。

新表进入模型时的默认桶是**永久**；把任何表移入可删除桶必须修订本 ADR。

## 3. 决策一：保留数字（待产品负责人逐项认可）

| # | 对象 | 策略 | 数字 |
|---|---|---|---|
| R1 | `RawDataRecord` 的响应正文 | 清空正文；行、`content_hash`、实际 byte 大小、`request_fingerprint`、来源 URL 与观察/解析链全部保留 | 180 天 |
| R2 | `Notification` | 物理删除 | 365 天 |
| R3 | `NotificationDeliveryAttempt` | 物理删除（先于或同事务随其 Notification） | 365 天 |
| R4 | 审计与领域历史：`AuditRecord`、`DataChange`、`SourceEvidence`、`SyncRun`、`RawDataObservation`、`RawDataParseAttempt`、`EarningsCalendarObservation`、`EarningsReconciliationDecision`、`InvestorRelationsObservation`、`InvestorRelationsDecision`、`FilingEarningsDecision`、`FilingEarningsLink`、`EarningsDateChange`、`EarningsEvent`、`Filing`、`FilingDocument`、`IndexChangeEvent`、`IndexChangeLeg`、`IndexMembership`、`MonitoringPoolSnapshot`、`MonitoringPoolMember`、`Company`、`SecurityListing`、`WatchlistItem` | **永久保留**；不得改写或删除（AGENTS.md §8 宪法约束，本 ADR 不构成任何例外） | 永久 |
| R5 | 数据库备份 | 每日备份，保留 30 天；平台映射 = Railway 卷备份 Daily（6 天）+ Weekly（27 天）+ Monthly（89 天）叠加 + 每周手动 `pg_dump` 逻辑导出（部署手册 §7） | 每日 / 30 天 |
| R6 | 账号删除与 watchlist 匿名化 | 不在本文范围；8.4 公开注册前另议（将产生新的决策记录） | 延后 |

R1 的「清空」是 `RawDataRecord`「不可原地修改」原则（data-sources §4.3）的
**唯一**例外：实现阶段将为该表新增 `payload_purged_at` 标记（迁移属实现阶段），
清空只允许把正文置空并打标记，任何其他字段、任何其他表不得借同一代码路径改写。
清空后的后果是：超过 180 天的窗口不再能从原文重放（replay 对已清空行 fail
closed）；哈希、大小与指纹仍可证明「曾经抓到过什么」。

## 4. 决策二：清理任务设计原则（本轮只落契约，实现属后续阶段）

1. **追加式实现**：清理任务本身是可审计的运维动作——每次运行创建
   `SyncRun`（建议 `job_type=maintenance.retention_cleanup`），通过
   `audit.services` 以系统身份追加 `AuditRecord`，记录截止点、扫描量与
   清理量；行级清空使用 `payload_purged_at` 标记而非隐式 NULL。清理过程
   不新增任何「改写历史」的写路径。
2. **幂等**：同一截止点重跑必须零二次效果——已打标记的行跳过、已删除的行
   不存在、计数可复算；「同一输入连续执行两次」自动测试是该实现阶段的验收
   条件（AGENTS.md §8）。
3. **审计可见**：清理不静默。Admin 可见运行记录与计数；删除按小批量分批
   （带 LIMIT 循环），不做一次性大事务 `TRUNCATE` 或无审计的批量
   `QuerySet.delete()`。
4. **顺序与完整性**：`NotificationDeliveryAttempt` 先于（或同事务随）其
   `Notification` 删除，避免悬挂外键；不触碰 R4 清单中的任何表。
5. **调度归属**：实现完成后注册每日一次低峰 cron（部署手册 §5 的 cron 清单
   届时更新）；本文不引入新的调度设施。

## 5. 备选方案与拒绝理由

- **原始正文外部对象存储归档**：满足更长保留，但引入新依赖、新账号与新
  凭据面；个人规模收益不抵复杂度。拒绝；容量压力出现时按 ADR-017 的对象
  存储评估路径另行评审。
- **正文保留缩短到 90 天**：90/30 天 reconciliation 窗口与年度追溯需要更长
  原文证据。拒绝。
- **通知记录永久保留**：个人规模下 365 天后的投递记录无使用场景，只增加
  备份体积。拒绝。
- **现在实现清理任务**：8.1 范围是部署准备；清理代码、迁移与 cron 注册
  独立成小阶段评审更安全。拒绝在本阶段实现。

## 6. 结果与未决

- 保留数字与桶边界成文，PD-6 的保留部分关闭；`docs/data-sources.md` §10 与
  `docs/architecture.md` §4.3/§14 对应待确认项同步关闭。
- 账号删除、watchlist 匿名化与用户数据导出：延至 8.4 前另议（PD-6 的剩余
  部分由新的决策记录关闭）。
- 实现阶段待办（后续小阶段，不在本文交付）：`payload_purged_at` 迁移、
  清理 management command、`maintenance.retention_cleanup` cron 注册、
  批量大小配置项与全部幂等/审计测试。

## 参考

- `AGENTS.md` §8（幂等、并发与历史）、§9（数据约束）
- ADR-023 §13（PD-6 出处）、ADR-025/ADR-027（通知与投递生命周期）
- `docs/data-sources.md` §4.3（原始数据保留基线）、§10（原待确认项）
- `docs/architecture.md` §4.3、§14（原待确认项）
- `docs/deployment.md` §7（备份策略的平台映射与恢复演练）
