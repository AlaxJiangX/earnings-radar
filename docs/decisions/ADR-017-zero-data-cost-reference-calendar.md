# ADR-017：Zero Data Cost Reference Calendar Layer

- 状态：已接受（产品范围 / 架构决策；不实现代码、schema 或迁移）
- 日期：2026-09-29
- 决策者：产品负责人
- 评估与起草：Codex（按 Zero Data Cost 产品范围 / 架构决策 round 执行）
- 评审基线：分支 `codex/4.2f-identity-strategy-review`，commit
  `77932b698b9ded8baaeb90be14f0a8b2d43b1738`；`origin/main`
  `ead1e18fce7fd667b039aff482fe63907957e3b6`
- 影响阶段：4.2F 拆分为 4.2F-A / 4.2F-B；ADR-015 canonical identity 契约不变

> 修订说明（ADR-020）：两层 reference + canonical 模型与 4.2F-A 边界保持不变。
> ADR-020 进一步冻结 Alpha Vantage Free v2 candidate-entry adaptation：candidate 允许
> `period_type = NULL` 并配合 promotion firewall；normalized / candidate pipeline storage
> 的许可仍待澄清，4.2F-B implementation 未开始。

## 1. Zero Data Cost 定义

本仓库不再使用含糊的 "zero cost"，统一使用 **Zero Data Cost**：

> Zero Data Cost 指项目在正常长期运行下，为覆盖配置的 Monitoring Pool 所需财报日历
> 数据访问、API 调用、数据订阅或数据许可，不需要支付任何经常性费用。

不计入 Zero Data Cost：开发时间、本地电脑与电力、服务器 / VPS、PostgreSQL 托管、
域名、GitHub、OpenAI / Codex / 模型调用、部署与一般软件工程成本。

```text
Zero Data Cost != Zero Total Cost
```

Trial、credits、sample-only symbols、需要升级才能覆盖全 pool 的 free tier，以及正常长期
使用会超过免费额度的 API，都不算 Zero Data Cost。

本决策要分开两个产品声明：

```text
Zero Data Cost + Full Monitoring Pool Visibility
Zero Data Cost + Full Monitoring Pool Canonical Coverage
```

## 2. 背景

- ADR-016 已把 Alpha Vantage Free 作为 4.2F **canonical primary Provider** 拒绝；
- 拒绝原因是 canonical 所需的 exact issuer identity 与 normalized `period_type` 不可得，
  窗口契约与 retention / replay 许可也未通过；
- 但该 Provider 的 3-month market-wide calendar、免费日额度与可用字段足以支撑一个
  display-only 的 reference calendar；
- 核心问题是：能否在不把每条 reference row 强行变成 canonical `EarningsEvent` 的前提下，
  提供有用的 full Monitoring Pool visibility。

## 3. Decision：两层模型

采用 **TWO-LAYER REFERENCE + CANONICAL**，三层职责如下：

```text
Layer A Reference / Estimated Calendar
- display-only reference rows
- Zero Data Cost 来源允许时覆盖 full Monitoring Pool 查找 / 过滤
- 可缺 canonical identity facts
- 明确标注 estimated / reference
- 不自动成为 canonical EarningsEvent

Layer B Canonical Earnings Events
- 只在 exact issuer + period identity facts 满足时进入
- 继续遵守 ADR-010 / ADR-013 / ADR-014 / ADR-015
- 无 fuzzy promotion；不完整行保持 reference / review-only

Layer C Higher-authority confirmation
- SEC / IR / 未来 licensed Provider
- 可确认或补充 canonical events（不在本轮范围内）
```

Option A（strict canonical-only）会牺牲产品可见性；Option C（放宽 canonical identity）
会破坏 ADR-015 / ADR-014，均不采用。

## 4. Reference row 的停止点（仓库验证结果）

已核对现有模型后确认：

| 模型 | 结论 | 原因 |
|---|---|---|
| `RawDataRecord` | 复用 | 保存 raw bytes、来源、抓取时间、内容哈希 |
| `RawDataObservation` | 复用 | 把 raw record 绑定到 SyncRun |
| `RawDataParseAttempt` | 复用 | 只保存 `(observation, parser_version)` 状态与错误摘要，不存行数据 |
| `EarningsCalendarObservation` | 不作为 reference 行存储 | `provider_event_id` 是 ADR-015 Layer 2 source identity；ADR-015 §6 禁止缺 exact issuer / period facts 时创建；canonical-eligible 行仍按其既有规则使用 |
| `EarningsReconciliationDecision` | 不复用 | 依赖 observation 与 canonical target，不是 reference 存储 |
| `EarningsEvent(candidate)` | 不复用 | candidate 仍需 Company 与 canonical period facts |
| `MonitoringPoolSnapshot` / `MonitoringPoolMember` | 复用 | frozen scope；basis 指向 as-of `SecurityListing` |
| `SecurityListing` | 复用 | 仅用于 display-only 的 symbol / ticker 解析 |

因此 reference row 的停止点是：

```text
RawDataRecord + RawDataObservation + RawDataParseAttempt(reference parser)
```

reference 层由只读 selector 在 persisted raw payload 上确定性重新解析并投影；它
MUST NOT 创建 `EarningsCalendarObservation`、`EarningsReconciliationDecision`、
`EarningsEvent(candidate)` 或 SourceEvidence。这样 reference / canonical 的边界是物理
隔离的：不完整行根本没有进入 canonical 流水线的入口。

现有结构可以安全表达该区分，所以 v1 不引入新模型。

即使 Provider 提供 native ID 且 ADR-015 §6 允许创建 observation，reference 投影也不依赖
该 observation，以保持 reference 与 canonical 的物理分离。

## 5. Reference 匹配规则（display-only）

- scope 只来自 run 已持久化的 frozen `MonitoringPoolSnapshot`（as-of / hash）；
- 解析键为 exact `provider_symbol` == `SecurityListing.ticker`（trim / uppercase 规范
  化），listing 必须来自该 snapshot member basis 且在该 as-of 有效；
- symbol 必须唯一解析到一个 Company；同一 Company 的多个 listing 可以视为一个结果；
  跨 Company 歧义必须 fail closed，不显示该行，也不得猜测交易所；
- 不做 fuzzy、相似度、name-only、release-date 或 exchange 推断；provider 特有符号别名
  规则如未来需要，必须单独批准并版本化；
- 该匹配只用于展示与 pool 过滤，MUST NOT 进入 candidate、reconciliation、promotion 或
  canonical identity 输入。

## 6. Reference Layer 契约

Reference rows MUST：

```text
明确标注 estimated / reference
不冒充 confirmed / released
不创建 canonical identity
不把 reportDate 当作 identity
不伪造 period_type
不做 destructive merge
不修改 confirmed / released / cancelled 状态
在许可允许时保留到 raw record / position / parser version 的来源追踪
```

Reference rows MAY：

```text
显示 upcoming estimated date
在 provider 提供时显示 time / session
按 frozen Monitoring Pool 过滤
随 provider 估计变化在下次投影更新
在 exact facts 补齐后仅通过 canonical 流水线变为 canonical-eligible
```

Provider absence 不触发 cancellation、deletion 或 status mutation。

## 7. Canonical eligibility（不变）

进入 candidate 之前仍要求：

```text
exact Company identity
period_end_date
normalized period_type
stable source event identity（provider-native 或 ADR-015 internal）
no unresolved identity conflict
```

ADR-015 / ADR-014 的 exact-only 规则不因 reference 层存在而放宽。

## 8. Period-Type handling

`period_type` 缺失时：

```text
reference visibility = allowed（由 reference 契约决定）
canonical eligibility = NO
```

不得默认赋 `QUARTER`，不得用 `fiscalDateEnding` 猜测。只有单独批准的 exact fiscal-period
规则才能把行升级为 canonical-eligible，且必须经过 canonical 流水线。

## 9. Licensing interaction

Zero Data Cost 只定义数据费用，不授予 retention、derived、display 或 replay 权利：

```text
Mode A Retention-compatible Provider
  raw persistence + deterministic read-time reference projection + offline replay
  + append-only raw evidence
  可以使用现有架构

Mode B Retention-restricted Provider
  production ingestion 继续 blocked
  除非未来 ADR 明确改变 retention 架构
```

ADR-016 对 Alpha Vantage Free 的结论不变：其数据在 3-month market-wide、免费额度与
reference 过滤层面可用，但 retention / display 权利未确认，属于 Mode B，因此在获得
书面授权前仍不得进入 production ingestion。Zero Data Cost 不等于许可批准。

## 10. 两个产品声明（不得混淆）

```text
Zero Data Cost Full Monitoring Pool Visibility
= YES（架构可行；前提是 Mode A Provider）
  当前 Alpha Vantage Free 运营状态 = NO（许可未通过）

Zero Data Cost Full Monitoring Pool Canonical Coverage
= NO / PARTIAL（尚未证明）
```

## 11. Schema / Migration

```text
schema change required = NO
migration required = NO
```

v1 reference 层完全落在既有 raw / parse-attempt / snapshot / listing 结构上。若未来需要
persisted reference history、reference 变更追踪或性能优化，必须另开 ADR 评估独立模型，
不得顺手扩展 `EarningsCalendarObservation`。

## 12. Roadmap Impact

4.2F 拆分为两个执行切片，避免把 reference visibility 与 canonical coverage 混在一起：

```text
4.2F-A Zero Data Cost Reference Calendar
4.2F-B Canonical Provider-Grade Live Sync
```

- 4.2F-A 的范围是 reference 投影、pool 过滤、UI 标注与 Mode A license gate；不创建
  observation / decision / candidate / EarningsEvent；
- 4.2F-B 保持原 4.2F 的 live Provider adapter 与 `earnings.calendar_window` sync 范围；
- 两个切片都保持 license gate；各自 planning gate 定义详细验收标准。

## 13. 本轮不做

- 不实现 Provider adapter、reference ingestion、sync command 或 runtime scheduling；
- 不新增 model、migration 或 DB 约束；
- 不放宽 ADR-015 / ADR-014 canonical identity；
- 不批准 Alpha Vantage，也不选择替代 Provider；
- 不修改 PRD；PRD §7.1 / §12.2 与 architecture §4.5 的 reference 措辞同步属于
  4.2F-A planning gate 的前置，需产品负责人明确指示后执行。

## 14. 复核条件

- Mode A license 结论与 4.2F-A planning gate 完成前，4.2F-A 保持 BLOCKED；
- 4.2F-B 需要能提供 exact issuer identity、normalized `period_type` 且许可覆盖
  retention / replay / display 的 Provider；
- 若 reference 层需要持久历史或变更追踪，重新评估 schema 决策。

## 参考

- `docs/decisions/ADR-010-earnings-calendar-observation-and-reconciliation.md`
- `docs/decisions/ADR-011-earnings-calendar-offline-replay.md`
- `docs/decisions/ADR-013-earnings-candidate-company-matching.md`
- `docs/decisions/ADR-015-system-owned-source-event-identity.md`
- `docs/decisions/ADR-016-alpha-vantage-free-provider-gate.md`
- `docs/architecture.md` §4.5
- `docs/data-sources.md` §2、§3
- `docs/development-roadmap.md` Stage 4.2F
