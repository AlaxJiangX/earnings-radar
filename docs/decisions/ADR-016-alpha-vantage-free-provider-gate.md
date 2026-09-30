# ADR-016：Alpha Vantage Free 4.2F Provider / License Gate 结果

- 状态：已接受（gate 结论为 REJECTED；ADR-020 已对 Alpha Vantage v2 candidate-entry 部分重评，见修订说明）
- 日期：2026-09-29
- 决策者：产品负责人
- 评估与起草：Codex（按 4.2F focused feasibility gate 任务执行）
- 评审基线：分支 `codex/4.2f-identity-strategy-review`，commit
  `77932b698b9ded8baaeb90be14f0a8b2d43b1738`；`origin/main`
  `ead1e18fce7fd667b039aff482fe63907957e3b6`；原始 workspace 的 `M Dockerfile` 未触碰
- 影响阶段：4.2F canonical-primary 保持 BLOCKED；AV v2 candidate-entry adaptation 由
  ADR-020 冻结，implementation 仍待许可澄清

> 修订说明（ADR-020）：ADR-020 在严格个人、私有、单用户、非商业的 candidate-only
> adaptation 范围内重新评估了本 ADR 的部分拒绝理由。AV v2 source identity、frozen
> snapshot symbol matching、forward-only window 与 candidate-only promotion firewall
> 已由 ADR-020 冻结；本 ADR 对 canonical-primary、公开、多用户、商业与再分发的拒绝
> 保持不变，许可澄清前的 implementation 仍保持 BLOCKED。

## 1. 范围与问题

本 ADR 只回答一个 focused gate 问题：Alpha Vantage Free 的 market-wide
`EARNINGS_CALENDAR` 是否满足 ADR-010 / ADR-015 下 Stage 4.2F 的必要条件：

1. 90-day market-wide calendar；
2. full monitoring-pool filtering（exact CIK，或 exact ticker + canonical exchange）；
3. 通过 frozen local `SecurityListing` 获得 exact issuer identity；
4. exact `period_end_date`；
5. safely obtainable `period_type`；
6. current personal / private use terms；
7. raw / normalized retention；
8. offline replay。

范围外：替代 Provider 调研、Provider adapter / command 实现、窗口或 identity 契约修改、
真实 API key、把真实第三方响应提交为 fixture。本 ADR 不做上述任何一项。

## 2. 证据（2026-09-29 抓取）

### 2.1 官方文档

`https://www.alphavantage.co/documentation/` 的 Earnings Calendar 章节：

- 只返回未来数据："This API returns a list of company earnings expected in the next 3,
  6, or 12 months."；
- `horizon` 只接受 `3month`（默认）/ `6month` / `12month`；
- 不指定 `symbol` 时返回 "the full list of company earnings scheduled"；
- 该章节标注 "Claim your free API key here"，且没有其他 Premium 章节所带的
  "premium API function" 提示。

### 2.2 只读 demo 端点证据

使用文档公开的 `apikey=demo` 调用一次（无真实 key；响应未写入仓库）：

- CSV header：`symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay`；
- 4,612 行、4,612 个不同 symbol；`(symbol, reportDate, fiscalDateEnding)` 无重复；
- `reportDate` 范围 `2026-09-28` ~ `2026-12-22`，没有任何过去日期；
- `fiscalDateEnding` 全部非空；`timeOfTheDay` 中 4,350 行为空、160 行 `pre-market`、
  102 行 `post-market`；
- 没有 CIK、exchange、fiscal quarter / fiscal year、period type 字段。

### 2.3 免费额度

`https://www.alphavantage.co/support/`："the majority of our datasets for 25 API
requests per day and unlimited API requests for verified open-source or educational
projects"。后者只涉及调用额度，不是数据使用许可。

### 2.4 Terms of Service

`https://www.alphavantage.co/terms_of_service/`（PDF；正文无显式版本号与生效日期；PDF
metadata 的 ModDate 为 `2026-08-27`）：

- Grant of License 限定为 personal, non-commercial use，除非另有书面约定；
- "commercial use" 定义包括 "any type of commercial activity that allows individuals
  or entities other than User to access information directly or indirectly"；
- license 是 non-exclusive / non-sublicensable / non-transferable / non-assignable /
  revocable；
- 全文没有 caching、persistence、derived data、retention、redistribution 或
  notification 的明示授权条款。

## 3. Provider / License Checklist（ADR-010 §7）

| 项目 | 结论 | 证据 / 说明 |
|---|---|---|
| provider | Alpha Vantage Inc.，`EARNINGS_CALENDAR` | market-wide earnings calendar |
| product / plan | Free API key | 25 requests/day；open-source / educational 可申请 unlimited（仅额度） |
| Terms URL / version / effective date | `/terms_of_service/`；无显式版本或生效日期；PDF ModDate `2026-08-27` | reviewed 2026-09-29 |
| API 使用许可 | Restricted：仅 personal, non-commercial | 其他用途需书面 / 商业协议 |
| caching 权 | Unconfirmed | ToS 无明示授权 |
| persistence 权 | Unconfirmed | 仓库要求 append-only raw 保留与 normalized 派生 |
| redistribution / public display 权 | Not granted | 多用户或公开访问即 AV 定义的 commercial use |
| notification / alert use 权 | Unconfirmed | 向他人发送数据即向其提供信息 |
| derived data 权 | Unconfirmed | ToS 无明示授权 |
| self-host / server-side use 权 | Restricted | 许可限定个人拥有或控制的设备、不可转让；多部署未授权 |
| rate limits | 25 requests/day | 市场级日历每天 1 次足够；per-symbol 补齐不可行 |
| retention restrictions | Unconfirmed | 无明示条款；未知时按仓库规则 fail closed |
| reviewer | Codex（按授权执行 focused gate） | 产品负责人未逐条签署；替代 Provider 选择仍属产品决策 |
| reviewed_at | 2026-09-29 | 同一次只读审查 |
| conclusion | **REJECTED** | 见 §4 与 §5 |

## 4. 逐项核对

| 需求 | 结论 | 依据 |
|---|---|---|
| 90-day market-wide calendar | FAIL（部分） | market-wide 为真；但 horizon 只有 forward 3 / 6 / 12 months；demo 覆盖 2026-09-28 ~ 2026-12-22（85 天），无精确 90 天参数，也无过去窗口 |
| full monitoring-pool filtering | FAIL | ADR-013 匹配要求 exact CIK 或 exact ticker + canonical exchange；AV 只有 symbol，无 CIK / exchange，无法安全过滤到 frozen pool |
| exact issuer identity via frozen local `SecurityListing` | FAIL | ADR-015 §5.3 禁止把 current monitoring pool state 作为 identity 输入；用 snapshot 补 exchange 会让 identity 依赖 pool / 本地主数据；ticker-only 不 exact（多交易所、多上市、`BRK.B` 类符号约定）；改变这一点需要新 ADR |
| exact `period_end_date` | PASS | `fiscalDateEnding` 为 `YYYY-MM-DD`，demo 中全部非空 |
| safely obtainable `period_type` | FAIL | payload 无 period type；仅凭 `fiscalDateEnding` 无法区分 Q4 与 FY（财年末同月），也无法覆盖 52/53 周日历；per-symbol `EARNINGS` / `EARNINGS_ESTIMATES` 补齐需要约 2.5k 次调用/天，远超 25/day，且属新增调用与许可风险；推断值不能当作 exact fact |
| current personal / private use terms | RESTRICTED | 纯个人非商用的私有监控可解释为许可范围内；但 retention 等权利未确认；任何他人可访问即 commercial |
| raw / normalized retention | FAIL / UNCONFIRMED | 仓库要求 append-only raw 与派生 normalized 长期保留；ToS 无明示授权，按数据源规则 fail closed |
| offline replay | FAIL / UNCONFIRMED | ADR-011 replay 只依赖 persisted raw；AV payload 缺 exchange / period_type，无法从 raw 确定性重建 source identity；retention 权利也未确认 |

## 5. Gate Decision

```text
FAIL — Alpha Vantage Free is REJECTED for Stage 4.2F.
4.2F remains BLOCKED.
```

硬阻塞（任一独立成立即不可通过）：

1. `period_type` 缺失，ADR-015 internal source identity 无法生成。按 ADR-015 §6，
   无 native ID 且 period facts 不完整的记录不得创建 `EarningsCalendarObservation`、
   candidate 或自动 reconciliation，链路没有可用输出。
2. issuer identity 缺失。AV 只有 symbol，没有 CIK / exchange；用 frozen pool 补齐被
   ADR-015 §5.3 的禁令与 replay 确定性要求挡住，不能作为 4.2F 的既定实现路径。
3. 窗口契约不可满足。provider 只提供 forward horizon，无法覆盖默认
   `past_correction_days=30`；`3month` 也不是精确 90 天。修改窗口属于产品决策，不在本轮范围。
4. license 不足。免费条款只覆盖个人非商用，且没有 caching / persistence / derived /
   retention / display / notification 的明示授权；未知项按仓库规则 fail closed。

明确排除的 workaround：用 pool snapshot 补 exchange / CIK；用 `fiscalDateEnding` 猜
`period_type`；用 per-symbol 调用补齐；改用 Alpha Vantage 其他端点；改用其他 Provider
（本轮不做 Provider 调研）。

## 6. 不改变的内容

- ADR-010 / ADR-011 / ADR-012 / ADR-013 / ADR-014 / ADR-015 契约不变；
- 4.2F 继续 BLOCKED；
- 不实现 Provider、sync command、model 或 migration；不写真实 API key；不提交真实
  provider fixture；
- 不选择替代 Provider，也不修改窗口或 identity 契约。

## 7. 解除阻塞需要

以下属于后续任务，且必须由产品负责人决策：

- 选择能提供（或经合法适配与 ADR 评审能补齐）exact issuer identity 与 normalized
  `period_type`，并覆盖 raw retention、derived data、offline replay、display 和
  notification 权利的 Provider；或
- 先由产品负责人修改窗口契约（例如明确允许 forward-only），再重新评估其后果；
- 新 Provider 仍须完成并通过 ADR-010 §7 的 provider / license checklist。

## 结果

- Alpha Vantage Free 的 4.2F focused gate 结论为 REJECTED；
- 阻塞来自字段、窗口与许可三类硬约束，不是缺少 Provider-native event ID；
- 4.2F 保持 BLOCKED，等待新的 Provider / License Gate 结论；
- 本拒绝只针对 4.2F 的 canonical primary Provider 角色，不排除未来在 Mode A
  retention-compatible 许可下作为 reference / estimated 来源（见 ADR-017）。

## 参考

- `docs/decisions/ADR-010-earnings-calendar-observation-and-reconciliation.md`（§7 gate、§8 record contract）
- `docs/decisions/ADR-011-earnings-calendar-offline-replay.md`（replay 定义）
- `docs/decisions/ADR-013-earnings-candidate-company-matching.md`（exact matching）
- `docs/decisions/ADR-015-system-owned-source-event-identity.md`（§5.2、§5.3、§6、§11、§14）
- `docs/architecture.md` §4.5（窗口与 monitoring pool 契约）
- `docs/data-sources.md` §2.2、§3（许可问题与 Provider 契约）
- `docs/development-roadmap.md` Stage 4.2F
