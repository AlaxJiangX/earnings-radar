# ADR-020：Alpha Vantage Free Canonical-Entry Adaptation Contract

- 状态：已接受（Stage 4.2F-B Alpha Vantage Adaptation Contract Gate；Gate = PASS after license resolution）
- 日期：2026-09-30
- 决策者：产品负责人（产品方向已批准；本 ADR 冻结技术契约边界）
- 评估与起草：Codex（按 `Finance_Stage_4.2F-B_Alpha_Vantage_Adaptation_Contract_Gate.md` 执行）
- 评审基线：`origin/main` `ffcb9bbf7a33e46b58f8bd54dee53fb64843d229`；分支
  `codex/4.2f-b-alpha-vantage-contract`
- 影响阶段：4.2F-B contract；implementation 仍受许可澄清阻塞，尚未开始

> Gate 结论（最终）：**技术契约 PASS；个人、私有、单用户、非商业的 normalized /
> candidate / canonical-pipeline storage 许可已由后续书面澄清解决**。初始 Gate 曾为
> PARTIAL PASS，唯一阻塞是 normalized / candidate pipeline storage 的许可覆盖；
> 该阻塞现已解除。本 ADR 只冻结契约，不包含生产实现；实现与验证由 Stage 4.2F-B
> Implementation + Verification 负责，公开 / 多用户 / 商业 / 再分发仍不在批准范围。

## 1. 背景与产品决定

ADR-016 曾以字段、窗口与许可三类硬约束拒绝 Alpha Vantage Free 作为 4.2F
canonical-primary Provider。ADR-019 随后仅批准个人、私有、单用户、非商业的 Mode A
reference 用途，并明确未批准 canonical 使用授权。

产品负责人现已批准新的方向：

> Alpha Vantage 可以在 fiscal facts 不完整的情况下进入主财报流水线；
> 缺失事实必须保持 unknown/null，不得伪造；
> Alpha Vantage 派生事件可以保持 CANDIDATE，直到 SEC / IR / approved manual evidence
> 等高 authority 来源补齐 canonical identity。

本 ADR 的目的不是重写 ADR-010 / 015 / 016，而是为 Alpha Vantage Free 冻结一个最小、安全、
可版本化的 adaptation contract，使不完整 fiscal facts 不削弱 Layer 3 canonical identity，
也不把缺口伪装成已知事实。

## 2. Gate 结论摘要

| 决策 | 结论 |
|---|---|
| canonical EarningsEvent identity | 不变：`Company + period_end_date + period_type` |
| Alpha Vantage 进入主流水线 | YES，但仅 candidate-only |
| Layer 2 source identity | 新增 v2，Company-scoped，namespace `internal:v2:` |
| issuer input | frozen `MonitoringPoolSnapshot` 解析出的 Company UUID |
| observation 允许 `period_type = NULL` | YES（仅 approved v2 provider） |
| candidate 允许 `period_type = NULL` | YES（仅 approved v2 provider） |
| `period_type = NULL` 自动 promotion | NO，严格禁止 |
| Company matching | frozen snapshot exact `provider_symbol` -> basis `SecurityListing.ticker` -> Company；歧义 fail closed |
| schedule authority | 仅 `estimated_release` / `release_session`，经既有 schedule service |
| confirmed / released / cancelled | Alpha Vantage MUST NOT 自动写入 |
| window | system desired coverage 与 provider capability 分离；AV 为 forward nominal 3month、past correction unsupported |
| replay | 仅使用 persisted raw + persisted frozen snapshot；network fetch = 0 |
| schema change required | NO |
| migration required | NO |
| license status | PASS（个人/私有/单用户/非商业的 normalized / candidate storage；公开/多用户/商业除外） |
| implementation status | NOT STARTED |

## 3. 保留的 Canonical 不变量

以下规则不因本 ADR 改变：

```text
Company
+ period_end_date
+ normalized period_type
= canonical EarningsEvent identity
```

CANONICAL `EarningsEvent` 必须同时具有：

```text
period_end_date != NULL
period_type != NULL
identity_key != NULL
identity_rule_version != NULL
```

MUST NOT：

```text
把 canonical period_type 改为 nullable
把 reportDate 放入 canonical identity
把 release date 当作 fiscal identity
把缺失 period_type 映射为 OTHER 以通过校验
仅凭 fiscalDateEnding 推断 Q4 / FY
削弱 canonical uniqueness
```

Alpha Vantage 适配只发生在以下层面：

```text
source identity
observation eligibility
candidate eligibility
provider window / capability
Company matching authority
promotion gate
reconciliation review behavior
```

## 4. 修订边界

本 ADR 只修订以下 ADR 的指定部分：

| 被修订 ADR | 被修订内容 | 保留内容 |
|---|---|---|
| ADR-010 | 第 2 节对 AV v2 的 issuer / source identity 规则；第 3 节对 AV v2 exact matching 的字段限制；第 5 节 universal 90 + 30 窗口；第 8 节 normalized record 的 `provider_event_id` 生成时点 | canonical identity、exact-only 原则、no destructive merge、manual authority、absence non-authoritative、license gate |
| ADR-013 | 第 8、9 节对 AV v2 的 provider-specific symbol matching 禁令 | frozen snapshot、exact-only、fail closed、candidate 复用现有 `EarningsEvent` |
| ADR-015 | 第 5、6、11 节对 AV v2 的 issuer input 与 incomplete period 规则 | 三层 identity 模型、v1 历史不变、canonical identity 分离、replay 语义 |
| ADR-016 | 仅对个人、私有、单用户、非商业的 candidate-entry adaptation 重新评估 | 公开 / 多用户 / 商业 / 再分发 / canonical-primary 拒绝不变 |
| ADR-014 | 为 incomplete candidate 补充“不得以 Company + period_end_date 自动视为 exact duplicate”的边界 | exact-only grouping、no destructive merge、manual authority、ADR-009 promotion |

ADR-001 / 007 / 008 / 009 / 011 / 012 / 017 / 018 / 019 的其余决策保持不变。

## 5. 目标流水线与两阶段归一化

Alpha Vantage v2 的目标流水线：

```text
Alpha Vantage EARNINGS_CALENDAR
  -> raw persistence（不变）
  -> parser attempt（provisional provider-neutral rows）
  -> frozen MonitoringPoolSnapshot exact Company resolution
  -> v2 source identity generation
  -> EarningsCalendarObservation（period_type 可为 NULL）
  -> EarningsEvent candidate（v2 family identity）
  -> estimated_release / release_session through schedule service
  -> later approved period_type completion
  -> existing ADR-009 promotion
  -> CANONICAL EarningsEvent
```

关键顺序变化：

- 通用 parser contract 在 parse 阶段要求 `provider_event_id`；AV v2 的
  `provider_event_id` 只有在 frozen snapshot Company resolution 后才能生成，因此
  AV v2 MUST 使用两阶段归一化：parser 先输出 provisional row，resolver 解析 Company，
  identity generator 生成 v2 identity，之后才写 `EarningsCalendarObservation`；
- provisional row MUST NOT 写入任何持久化 source identity 字段；
- unresolved / ambiguous row MUST NOT 创建 observation、candidate、SourceEvidence 或
  auto-reconciliation；raw payload、RawDataObservation、RawDataParseAttempt 和
  SyncRun diagnostics 保留；
- Company resolution 与 identity generation MUST 在持久化 observation 前完成，避免
  append-only observation 的身份被后续修改。

现有 generic `persist_earnings_calendar_parse_result(...)` 与
`create_earnings_candidate_for_observation(...)` MUST NOT 被 AV v2 原样复用：
前者要求 parse 阶段已有 `provider_event_id`，后者按 observation 派生 candidate identity。
implementation 阶段必须提供 provider-specific v2 normalization / candidate service，
并保持 v1 generic paths 不变。

## 6. Alpha Vantage v2 Source Event Identity

### 6.1 版本与命名空间

```text
EARNINGS_SOURCE_EVENT_IDENTITY_VERSION_V2 =
  "earnings-source-event-identity-v2"

storage:
  internal:v2:<64 位小写 SHA-256>
```

v2 identity 使用既有 `EarningsCalendarObservation.provider_event_id` 物理列存储；列名是
命名债，不改变其 source event identity 语义。

### 6.2 必需输入与禁止输入

v2 canonical JSON 至少覆盖：

```text
source_event_identity_version
source_key
provider_key
company_id（resolved Company UUID 的规范化字符串）
period_end_date
```

规范化示例：

```json
{
  "source_event_identity_version": "earnings-source-event-identity-v2",
  "source_key": "<DataSource.key>",
  "provider_key": "alpha-vantage-free",
  "company_id": "<uuid>",
  "period_end_date": "YYYY-MM-DD"
}
```

MUST NOT 作为 v2 identity 输入：

```text
provider_symbol / ticker / exchange
reportDate / estimated_release
release_session
raw_position
parser_version
monitoring pool as-of / pool hash / selector version
fiscal_year / period_type / fiscal_calendar_type / period_length_weeks
company_name
database row ordering / created_at / wall clock
```

`company_id` 只允许来自当前 run 已持久化的 frozen `MonitoringPoolSnapshot` resolution；
MUST NOT 读取 today's live pool、current `MarketIndex.is_enabled` 或未持久化的内存对象。
Company 的 ticker rename、exchange change 或 listing successor 不改变 `Company.id`，因此
v2 identity 在 ticker 变化时保持稳定。

对 v2 issuer input 与稳定性的直接回答：

- `Company` UUID 是 v2 可接受的 issuer input；CIK / ticker / exchange 不再是该 approved
  path 的必需输入，但它们也不得替代 frozen snapshot resolution；
- 在本 approved path 中，resolved Company UUID + `period_end_date` 足以构成 Layer 2
  source identity；单独 `period_end_date` 不足以跨 issuer 使用；
- Company merge / split 当前不存在；若未来引入，必须另开 ADR 评估 v2 identity 的稳定性，
  MUST NOT 通过批量改写历史 observation 解决。

### 6.3 Collision、重复行与 period_end_date 边界

由于 AV v2 不包含 period_type，v2 identity 只能表达：

```text
source + provider + resolved Company + period_end_date
```

它不能区分理论上可能共享同一 period end 的多个 fiscal events。因此：

- 同一 v2 identity 出现 conflicting fiscal facts 时 MUST fail closed 到
  `collision` / `review_required`，MUST NOT 静默 fork 第二个 canonical event；
- 同一 raw payload 内两条 provisional row 若解析为同一 v2 identity：
  - identity-relevant facts 与 schedule facts 完全一致时，MAY 按最小 `raw_position`
    确定性去重，并记录 duplicate count；
  - facts 不一致时 MUST NOT 任选一条写入 observation；该组只保留 raw / parse /
    diagnostic lineage；
- 同一 resolution key（`source_key + provider_key + normalized provider_symbol +
  period_end_date`）在不同 run 解析到不同 Company 时 MUST 写
  `collision` / `review_required`，MUST NOT 创建第二个 candidate 或静默改写既有映射；
- 同一 v2 identity 在不同 raw / parser revision 中重复出现时，observation
  append-only 保留；candidate family 复用见第 9 节。

### 6.4 period_type completion 与 lineage

v2 identity 明确排除 `period_type`，因此后续 approved source 补齐
`period_type` 时：

```text
source lineage 不变
observation / candidate 关系不重写
candidate row 可在受控 completion flow 中补齐 identity facts
promotion 仍通过 ADR-009
```

不得为 completion 创建新的 v2 identity；不得覆盖旧 observation；不得把
`fiscalDateEnding` 推断出的 period label 作为 completion 输入。

### 6.5 v1 / v2 共存

- 既有 `internal:v1:*` observation MUST 保持原样，不回填、不重算、不重命名；
- v2 只用于本 ADR 批准路径；同一 provider event MUST NOT 在 v1 与 v2 之间来回切换；
- v1 与 v2 通过受控 namespace 区分；若未来需要把 v1 历史迁移到 v2，必须另开 ADR；
- v2 的 hash payload version 与 storage prefix version 必须一致；算法升级必须使用
  `internal:v3:` 或新的版本前缀。

## 7. Frozen-Snapshot Company Resolution

AV v2 不允许 CIK 或 exchange 参与 generic matcher；它使用 provider-specific matching
authority：

```text
normalized provider_symbol
  -> exact match against SecurityListing.ticker
     referenced by persisted frozen MonitoringPoolSnapshot member basis
  -> resolve to Company
```

规则：

- `provider_symbol` 规范化为 trim + uppercase；ticker 本身必须已经是
  `companies.services.normalize_ticker` 形式；
- 只允许使用 snapshot member `basis` 指向的 `SecurityListing`；listing 必须在
  `monitoring_pool_as_of` 有效；
- 0 Company match：不创建 observation / candidate，保留 raw / parse lineage；
- exactly 1 Company：issuer resolved；
- 同一 Company 多个 listing 或 share class：视为一个 Company，去重记录 listing evidence；
- matches across > 1 Company：ambiguous，fail closed；
- snapshot member、basis listing 或 Company 缺失、hash mismatch、as-of 无效：hard fail；
- MUST NOT 使用 fuzzy name、substring ticker、current/live pool、guessed exchange、
  unapproved symbol alias 或 `provider_symbol` generic fallback。

冻结的 provider-specific matcher 版本：

```text
ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2 =
  "alpha-vantage-company-match-v2"
```

该 matcher 只适用于本 ADR 批准的 Alpha Vantage v2 path；generic matcher 不得因此放宽。

Resolution evidence MUST 记录在 candidate decision 的受控 `match_factors` namespace 中，
至少包括：

```text
matcher_version
normalized provider_symbol
frozen snapshot id / as-of / selector_version / pool_hash / input_revision
resolved Company id
matched SecurityListing ids
resolution outcome
reason_code
```

## 8. Observation Eligibility

当且仅当以下条件同时满足时，AV v2 MAY 创建 `EarningsCalendarObservation`：

```text
provider approved for v2 incomplete-period identity
no Provider-native event ID required
Company resolved safely from frozen snapshot
period_end_date present
```

随后允许：

```text
period_type = NULL
fiscal_year = NULL（除非 payload 明确提供 exact fiscal year）
fiscal_calendar_type = UNKNOWN（NULL source fact 的领域表示）
period_length_weeks = NULL
includes_q4 = false
```

现有 schema 已允许 observation `period_type` 为 NULL，并有对应 CheckConstraint；
因此本 ADR 不需要 schema change。MUST NOT 把缺失 `period_type` 映射为 `OTHER`、`FY`
或任何具体 period；MUST NOT 用 raw position、reportDate、fiscalDateEnding 生成
canonical period identity。

`resolution outcome = unmatched / ambiguous` 的行不满足 observation eligibility：

- unmatched / out-of-pool：合法排除，记录 excluded count；
- ambiguous / integrity conflict：记录 review-required diagnostic，fail closed；
- raw payload、RawDataObservation、RawDataParseAttempt、SyncRun summary 必须保留；
  如需 per-row 结构化 resolution decision，必须另开 schema ADR，本 ADR 不新增模型。

## 9. Candidate Eligibility、Candidate Family 与 Schedule Authority

### 9.1 Candidate eligibility

以下组合允许创建 candidate：

```text
Company known
period_end_date known
period_type unknown（NULL）
```

Candidate 必须保持：

```text
identity_status = candidate
identity_key = NULL
identity_rule_version = NULL
status = scheduled_estimated
```

AV v2 MUST NOT 自动写：

```text
confirmed_release
earnings_release
conference_call
scheduled_confirmed
released
cancelled
```

`estimated_release` 与 `release_session` 只允许通过既有 schedule domain service 写入：

```text
reportDate -> estimated_release，precision = date_only
timeOfTheDay -> release_session
  空值 -> unknown
  允许映射 pre_market / after_market / unknown
```

Provider absence MUST NOT 触发 cancellation、deletion 或任何 status mutation。

### 9.2 Candidate family identity

为避免每日重复 fetch 为同一 logical event 创建多条 uncontrolled candidate，AV v2 使用
独立的 candidate family identity：

```text
EARNINGS_INCOMPLETE_CANDIDATE_IDENTITY_VERSION_V2 =
  "earnings-incomplete-candidate-identity-v2"
```

canonical JSON 至少覆盖：

```text
candidate_identity_version
source_event_identity（internal:v2:<sha256>）
source_key
provider_key
company_id
period_end_date
matcher_version（alpha-vantage-company-match-v2）
```

MUST NOT 覆盖：

```text
observation id
raw_data_record id
parser_version
snapshot as-of / pool hash / selector version
estimated_release / release_session
wall clock
```

Candidate UUID 通过固定 namespace + family key 的确定性 UUIDv5 派生；同一 family 重复
observation MUST 复用同一 candidate row，MUST NOT 创建第二个 candidate。

### 9.3 Repeated observation 与 schedule update

同一 family 的新 observation：

1. 重新验证 frozen snapshot resolution 与 v2 identity；
2. 写新的 append-only decision（`decision_type = matched_candidate`，
   `status = resolved`）指向既有 candidate；
3. schedule facts 有变化时通过 `update_earnings_schedule` 写 DataChange /
   EarningsDateChange / AuditRecord；无变化时不写 no-op；
4. MUST NOT 改写既有 observation、旧 decision 或旧 schedule history。

同一 family 首次创建 candidate 时使用 `created_candidate` / `resolved` decision；
冲突映射使用 `collision` / `review_required` / `open`，target 为 NULL；所有 decision
保持 append-only。

## 10. Promotion Firewall

严格规则：

```text
period_type = NULL
=> candidate MUST NOT auto-promote to canonical
```

Alpha Vantage ingestion MUST NOT 调用 `promote_earnings_event`，MUST NOT 传递推断的
`period_type`，也 MUST NOT 用 `fiscalDateEnding` 人工构造 Q4 / FY。

允许补齐 `period_type` 的 sources 仅限：

```text
SEC-derived exact facts
IR exact facts
approved manual evidence
future provider with exact normalized period_type
```

completion 必须通过受审计的 identity completion flow 写入 exact `period_type`；只有在
candidate 已具备 Company、`period_end_date`、`period_type` 后，才允许调用 ADR-009
`promote_earnings_event`。Alpha Vantage ingestion MUST NOT 调用其中任一步。本 ADR 只冻结
集成契约，不实现 SEC / IR completion，不新增 completion model；canonical collision MUST
fail closed，MUST NOT destructive merge。

## 11. Window Capability 契约

ADR-010 的 `forward_horizon_days = 90` / `past_correction_days = 30` 被重新解释为
**system-level desired coverage**，而不是对所有 Provider 的 universal hard requirement。

```text
system desired coverage
  forward_horizon_days = 90
  past_correction_days = 30

provider capability declaration（Alpha Vantage Free）
  forward capability = nominal 3month
  past correction capability = unsupported
  effective past_correction_days = 0
  coverage exactness = nominal
  provider coverage end = UNKNOWN
```

规则：

- AV normal sync 是 forward-only；
- `3month` MUST NOT 被声明为 exactly 90 days；
- MUST NOT 从最后一条 event 日期推断 provider coverage end；
- provider absence、窗口尾端没有数据或合法空日历 MUST NOT 触发删除、取消或 canonical
  identity mutation；
- historical / correction facts 未来由 SEC、IR、manual correction 或另一个 approved
  provider 提供；
- SyncRun scope / window contract 必须记录 provider capability 与 nominal coverage
  语义，并使用 versioned、deterministic 的 window canonicalization；本 ADR 不改变
  schema，scope 仍使用既有 JSON 结构。

建议的 v1 确定性 envelope：

```text
window_start = run business date（America/New_York，禁止 wall-clock 隐式读取）
window_end = add 3 calendar months with end-of-month clamping, minus 1 day
coverage exactness = nominal
```

该 envelope 只表达请求范围，不表达 provider 已覆盖整个区间。

## 12. Replay 与 Frozen Snapshot 语义

非协商规则：

```text
historical replay MUST use persisted raw + persisted frozen snapshot
MUST NOT resolve an old Alpha Vantage symbol using today's live monitoring pool
```

同一 persisted inputs 必须得到同一结果：

```text
same persisted raw
+ same parser version
+ same source-identity version
+ same frozen pool snapshot
= same observation / company resolution / source identity
```

实现必须：

- 按主键重新加载 source run、raw observation、raw payload 和 frozen snapshot；
- 重新执行 parser、Company resolution、v2 identity generation；
- 将重算的 v2 identity 与 persisted observation 的 `provider_event_id` 比较；
  不一致时 fail closed，不覆盖旧 observation；
- network fetch = 0；
- parser version 变化形成 append-only revision；旧 observation / candidate 保留；
- 不得用 current provider response、today's pool、current listing state 或内存对象替换
  persisted evidence。

## 13. Reconciliation 适配（Incomplete Candidate）

现有 exact-only reconciliation 保持保守。对 incomplete candidate：

```text
period_type = NULL
```

MUST NOT：

```text
仅因 Company 相同、period_end_date 相同就视为 canonical exact duplicate
destructive merge
premature exact canonical reconciliation
绕过 manual authority 自动 promotion
```

允许：

```text
按 v2 candidate family 查询与复用
进入 append-only review / collision / conflict decision
等待 approved period_type completion
completion 后进入 ADR-014 / ADR-009 既有路径
```

若 completion 后发现 canonical 或 candidate collision：

```text
fail closed
写 append-only collision / review decision
保留 loser 与全部历史
不删除、不覆盖、不复制历史
```

同一 incomplete candidate family 的重复 observation 复用既有 candidate；跨 family 的
自动 dedup 仍要求 Company + period_end_date + period_type 完整一致。

Incomplete candidate 的查询契约：

```text
identity_status = candidate
period_type IS NULL
Company
period_end_date
source event identity v2 / candidate family key
```

完成 exact period_type 后，candidate 保持原 UUID，由 ADR-009 在同一 row 上补齐
identity facts；如 completion 揭示 canonical / candidate collision，必须进入 review，
MUST NOT destructive merge。

## 14. License Boundary

### 14.1 初始结论（历史）

本 ADR 初始 Gate 结论为 PARTIAL PASS。其原因是当时的 Alpha Vantage Support 书面回复
明确覆盖：

```text
raw CSV / JSON persistence
historical retention
offline replay
derived fields
private single-user display
```

但没有明确覆盖：

```text
parsing stored responses into normalized database records
linking those records to local company / security master data
retaining normalized records long-term
using them internally as candidate earnings-event records
later completing those candidates with facts from other sources
```

ADR-019 也把 canonical 使用授权排除在结论之外。因此初始结论为：

```text
technical contract = PASS（本 ADR）
license activation for normalized / candidate / canonical-pipeline storage =
  BLOCKED pending Provider clarification
```

### 14.2 后续书面澄清与许可解决

产品负责人报告：Alpha Vantage Support 随后针对同一 strictly personal / private /
single-user / non-commercial 使用，书面批准以下用途：

```text
parse stored EARNINGS_CALENDAR responses into normalized database records
link normalized records to local company / security master data
retain normalized records long-term
use them internally as candidate earnings-event records
later complete those candidates with facts from other sources
```

边界保持不变：

```text
MUST NOT redistribute
MUST NOT sell
MUST NOT publicly display
MUST NOT make accessible to another user
MUST NOT use for a multi-user or commercial service
```

该确认由产品负责人转述；本 ADR 不引用未提供的原文，也不扩大其范围。因此最终状态为：

```text
technical contract = PASS（本 ADR）
license activation for normalized / candidate / canonical-pipeline storage,
strictly personal/private/single-user/non-commercial = PASS
public / multi-user / commercial = OUT OF SCOPE（需要另行书面协议）
```

初始 Gate 使用的澄清问题保留如下，作为历史记录：

> For the same strictly personal, private, single-user, non-commercial use:
>
> May I parse stored EARNINGS_CALENDAR responses into normalized database records,
> link those records to my own local company/security master data, retain those
> normalized records long-term, and use them internally as candidate earnings-event
> records that may later be completed with facts from other sources?
>
> No Alpha Vantage data will be redistributed, sold, publicly displayed, or made
> accessible to any other user.

公开、多用户、商业、再分发或客户访问场景仍必须重新执行 license gate；当前 PASS 只
覆盖本 ADR §1 所述个人、私有、单用户、非商业范围。

## 15. Schema / Migration Decision

```text
schema change required = NO
migration required = NO
```

依据：

- `EarningsCalendarObservation.period_type` 已 nullable，并有合法枚举 CheckConstraint；
- `provider_event_id` 已是非空 source event identity 物理列，可容纳 `internal:v2:`；
- `EarningsEvent` 已允许 candidate `period_type = NULL`，并在 canonical completeness
  constraint 中要求 promotion 后三要素完整；
- 现有 `EarningsReconciliationDecision` 已包含 `created_candidate`、
  `matched_candidate`、`collision`、`review_required` 等 append-only decision；
- SyncRun scope 是 JSON，job type 是受控字符串，可表达 provider capability 与 nominal
  window 语义；
- RawDataParseAttempt 绑定 RawDataObservation，可在不创建 observation 的情况下保留
  parse / resolution diagnostic。

如果实现发现必须新增 per-row resolution decision、provider capability 或 window
metadata 的结构化持久化，必须停止并另开 schema ADR；不得在本阶段顺手加模型或 migration。

## 16. Implementation Acceptance Tests

下一阶段 implementation + verification 至少覆盖以下矩阵。

### 16.1 Source identity

```text
same source + same resolved Company + same period_end_date
  => same internal:v2 identity

reportDate change
  => same source identity

release_session change
  => same source identity

provider_symbol / ticker rename with same resolved Company and period_end_date
  => same source identity

different Company
  => different source identity

different period_end_date
  => different source identity

v1 identity historical rows
  => unchanged / not rewritten
```

### 16.2 Matching

```text
unique frozen-pool symbol -> Company
same-Company multi-listing -> dedupe
cross-Company symbol ambiguity -> fail closed
out-of-pool -> no observation / no candidate
symbol reuse resolving to different Company across runs -> collision / review
replay uses frozen snapshot, not current pool
```

### 16.3 Observation / Candidate

```text
period_type NULL observation allowed
period_type NULL candidate allowed
candidate identity_key remains NULL
candidate identity_rule_version remains NULL
no auto-promotion while period_type NULL
same candidate family across repeated observations -> one candidate
estimated_release / release_session changes -> schedule service only
no confirmed / released / cancelled writes
```

### 16.4 Canonical firewall

```text
Alpha Vantage alone cannot create canonical EarningsEvent
Alpha Vantage alone cannot fabricate period_type
Alpha Vantage ingestion never calls promotion
Alpha Vantage absence cannot cancel or delete
canonical collision fails closed
```

### 16.5 Window

```text
forward-only Alpha Vantage capability
no false exact-90-day guarantee
no past correction claim
legal empty response succeeds without mutation
nominal coverage end remains UNKNOWN
```

### 16.6 Replay

```text
same persisted evidence -> same Company resolution / v2 identity
recomputed identity must equal persisted observation identity
network fetch = 0
parser-version revision is append-only
frozen snapshot is reloaded, never recomputed
```

### 16.7 Schema regression

```text
no new model
no new migration
canonical completeness constraint remains enforced
observation / decision append-only behavior remains
```

## 17. 明确 Non-Goals

本 ADR 不实现：

```text
Alpha Vantage canonical Provider adapter
live HTTP transport 或 management command
v2 parser / resolver / identity generator 生产代码
SEC / IR period_type completion
new model / migration / DB constraint
公开、多用户、商业或再分发用途
Stage 4.3 或任何后续阶段
```

## 18. Gate Decision

```text
PASS

technical contract = FROZEN
license activation for the strictly personal/private/single-user/non-commercial
normalized / candidate / canonical-pipeline storage = RESOLVED
next stage = Stage 4.2F-B Implementation + Verification
```

理由：

- canonical identity 未削弱；
- AV 可进入 candidate-only 主流水线，且缺失 period_type 保持 NULL；
- v2 source identity、冻结 snapshot matching、candidate family 与 promotion firewall
  都已冻结；
- schema / migration 均不需要；
- 初始 PARTIAL PASS 的唯一剩余阻塞（normalized / candidate pipeline storage 许可）
  已由后续书面澄清解决；
- 公开、多用户、商业与再分发仍不在批准范围内，必须重新审查。

## 19. 参考

- `docs/decisions/ADR-010-earnings-calendar-observation-and-reconciliation.md`
- `docs/decisions/ADR-013-earnings-candidate-company-matching.md`
- `docs/decisions/ADR-014-earnings-reconciliation-dedup-conflict-review.md`
- `docs/decisions/ADR-015-system-owned-source-event-identity.md`
- `docs/decisions/ADR-016-alpha-vantage-free-provider-gate.md`
- `docs/decisions/ADR-019-alpha-vantage-mode-a-reference-license-gate.md`
- `docs/architecture.md` §4.5
- `docs/data-model.md` §6.3、§6.4、§6.6、§6.7
- `docs/data-sources.md` §2、§3、§4.2、§8
- `docs/development-roadmap.md` Stage 4.2F-B
