# ADR 索引

本索引是 ADR 的导航层，不复制结论细节：一句话概括 + 影响范围 + 修订状态。
具体规则一律以 ADR 正文为准；被修订条款以修订方 ADR 为准。

用途（见 `AGENTS.md` 第 2 节定向阅读）：任务开始前按"主要影响"列筛选与本次改动 app
相关的 ADR 完整阅读；高风险任务仍需全量阅读全部 ADR。

维护规则：新增或修订 ADR 时，必须在同一任务中更新本索引（`AGENTS.md` 第 10 节）。

| ADR | 决策一句话 | 主要影响 | 状态/修订 |
|---|---|---|---|
| [ADR-001](ADR-001-earnings-event-identity.md) | EarningsEvent 正式身份 = company + period_end_date + period_type，候选身份分离；FY 含 Q4，52/53 周用专门字段 | earnings（4.1+） | 已接受 |
| [ADR-002](ADR-002-index-migration-rules.md) | 指数成员挂 SecurityListing；底层事实只有 ADDED/REMOVED；同日合并、1–7 日待复核；偏移方向与 ENTERS/REENTERS 规则 | indexes、companies | 已接受 |
| [ADR-003](ADR-003-release-filing-classification.md) | 财报状态机不含 FILED；release filing 用 YES/NO/REVIEW_REQUIRED 分类 | earnings、filings、notifications | 已接受；release 证据细节由 ADR-021 关闭 |
| [ADR-004](ADR-004-alpha-beta-gates.md) | 五类数据新鲜度初始目标与 alpha/beta 升级门槛 | 运维/发布（7.3、8.x） | 已接受；个人规模口径下进入 8.1 规划前按路线图待定决策表复核 |
| [ADR-005](ADR-005-fixture-first-index-domain.md) | 真实指数 Provider 未通过许可门前，领域只按 fixture-first 契约开发 | indexes、providers | 已接受 |
| [ADR-006](ADR-006-cross-date-correlation-model.md) | 跨日期指数变化的关联模型与候选持久化 | indexes（3.3） | 已接受 |
| [ADR-007](ADR-007-earnings-date-change-precision.md) | 财报日期按 date/盘前盘后/时刻分层保存精度，不伪造 UTC midnight | earnings（4.1B+） | 已接受 |
| [ADR-008](ADR-008-earnings-status-lifecycle-cancellation.md) | EarningsEvent 状态生命周期与取消语义 | earnings（4.1C+） | 已接受；取消权威由 ADR-022 补充 |
| [ADR-009](ADR-009-earnings-candidate-promotion.md) | 候选事件晋升为正式身份的契约 | earnings（4.1D、4.2） | 已接受 |
| [ADR-010](ADR-010-earnings-calendar-observation-and-reconciliation.md) | 日历 Observation、外部身份与 exact-only reconciliation 契约 | earnings、providers（4.2） | 已接受；source identity 由 ADR-015/020 修订，authority 由 ADR-021/022 补充 |
| [ADR-011](ADR-011-earnings-calendar-offline-replay.md) | 4.2C 离线 replay 契约 | earnings（4.2C） | 已接受 |
| [ADR-012](ADR-012-monitoring-pool-selector.md) | Monitoring-Pool selector 契约 | earnings（4.2D） | 已接受；Open Decision #3 由 ADR-023 解决 |
| [ADR-013](ADR-013-earnings-candidate-company-matching.md) | 候选创建与公司匹配：exact-only、fail-closed | earnings、companies（4.2D） | 已接受；Alpha Vantage 例外见 ADR-020 |
| [ADR-014](ADR-014-earnings-reconciliation-dedup-conflict-review.md) | Reconciliation、去重、冲突复核与人工权威 | earnings（4.2E/F） | 已接受 |
| [ADR-015](ADR-015-system-owned-source-event-identity.md) | 系统自有 source event identity（不依赖上游稳定 ID） | earnings、providers（4.2） | 已接受；AV 例外由 ADR-020 修订 |
| [ADR-016](ADR-016-alpha-vantage-free-provider-gate.md) | Alpha Vantage Free v1 gate 结论 REJECTED | providers（4.2F） | 已接受；v2 部分重评见 ADR-020 |
| [ADR-017](ADR-017-zero-data-cost-reference-calendar.md) | 零数据成本参考日历层（范围/架构决策，不落代码） | 产品/架构 | 已接受 |
| [ADR-018](ADR-018-reference-calendar-4.2f-a-contract.md) | 4.2F-A 参考日历契约 | earnings、providers（4.2F-A） | 已接受 |
| [ADR-019](ADR-019-alpha-vantage-mode-a-reference-license-gate.md) | Alpha Vantage Mode A 许可 gate：PASS，限个人/私有/单用户参考用途 | providers | 已接受 |
| [ADR-020](ADR-020-alpha-vantage-canonical-entry-adaptation.md) | Alpha Vantage v2 canonical-entry 适配契约（已实现，PR #51） | providers、earnings（4.2F-B） | 已接受 |
| [ADR-021](ADR-021-filing-earnings-link-classification-matching-review-replay.md) | Filing↔Earnings 链接、release 分类、匹配、复核与 replay 契约（EX-99.1/EX-99 allowlist） | filings、earnings（4.5A） | 已接受；4.5A 已实现 |
| [ADR-022](ADR-022-ir-confirmation-authority-cancellation-observation.md) | IR 确认、字段级 authority、取消与 Observation 契约 | earnings（4.5B） | 已接受；fixture-first 已实现，live 来源受 allowlist/许可 gate 阻塞 |
| [ADR-023](ADR-023-watchlist-monitoring-pool-contract.md) | Watchlist 域与 Monitoring-Pool v2 组合契约（§13 含产品未决项） | watchlists、earnings（5.1A/B） | 已接受；5.1A/B 已实现并验证 |
| [ADR-024](ADR-024-advance-reminder-anchor-and-rule-resolution.md) | 提前一天提醒按美东自然日锚定；P0–P3 规范矩阵；公司规则整体替换用户默认 + 双门抑制 | notifications（6.1+）、watchlists、earnings | 已接受 |
| [ADR-025](ADR-025-notification-enqueue-idempotency.md) | 通知入队：同事务 outbox、稳定业务键幂等/和解、7 类事件映射；PD-4 关闭为发送前复查 | notifications（6.2+） | 已接受；ADR-023 §13 PD-4 随之关闭 |
| [ADR-026](ADR-026-notification-wiring-points.md) | 通知接线：earnings/filings/indexes 同步编排层同事务调用 dispatch 的定向例外与接线点清单 | earnings、filings、indexes、notifications（6.2W） | 已接受 |
| [ADR-027](ADR-027-email-delivery-digest-retry.md) | 邮件投递：通用 SMTP 零依赖、P3 按用户本地日桶聚合（键 v2）、3 次尝试/15 分钟重试与永久失败停止、发送前复查三门与 in_app 统一 sent 语义 | notifications（6.4+） | 已接受 |
| [ADR-028](ADR-028-data-retention-and-cleanup-contract.md) | 数据保留：RawData 正文 180 天清空（行/哈希/大小保留）、通知与投递尝试 365 天删除、追加式审计与领域历史永久保留、备份每日 30 天；清理任务只落契约（追加式/幂等/审计可见），实现属后续 | audit、notifications、earnings、运维（8.1+） | 已接受（2026-10-25）；PD-6 保留部分关闭，账号删除/匿名化延至 8.4 前另议 |
