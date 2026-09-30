# ADR-015：System-Owned Source Event Identity

- 状态：已接受
- 日期：2026-09-29
- 决策者：产品负责人
- 影响阶段：4.2A-4.2F 的 source identity 解释，并为 4.2F Provider / License Gate 提供稳定输入
- 评审基线：`origin/main`，commit `ead1e18fce7fd667b039aff482fe63907957e3b6`

> 修订说明（ADR-020）：Alpha Vantage Free 的 candidate-entry adaptation contract 已由
> ADR-020 接受。第 5 节 internal identity 的 issuer 输入、第 6 节 incomplete input
> behavior 与第 11 节 zero-cost provider compatibility，对 Alpha Vantage Free v2
> approved path 由 ADR-020 部分修订；三层 identity 模型、v1 历史不变、canonical
> identity 分离、replay 语义与未列明规则保持不变。

## 1. 背景

ADR-010 要求 earnings calendar Provider 提供稳定、非空、由上游提供的
`provider_event_id`；无法提供时不得创建 `EarningsCalendarObservation`、candidate 或进入
automatic reconciliation。该规则在 Stage 4.2F Provider / License Gate 中成为硬阻塞：
现实中的零成本 / 低价 Provider 通常只提供 symbol、fiscalDateEnding、release date 和时段，
不提供 Provider-native 事件 ID。

本 ADR 回答一个问题：Finance 是否必须依赖 Provider-native 事件 ID，还是应该自己拥有
source event identity，并把 Provider-native ID 降为可选 lineage evidence。

本 ADR 只修订 identity contract，不实现 Provider adapter、不修改生产代码、不创建迁移、
不接入真实网络，也不批准任何具体 Provider。

## 2. 三层身份模型

必须区分以下三层：

| 层 | 回答的问题 | 身份 | Owner |
|---|---|---|---|
| Layer 1: raw record identity | 这是哪次网络获取的哪段原始正文？ | `RawDataRecord.source + request_fingerprint + content_hash`；`RawDataObservation(sync_run, raw_data_record)` | 系统 |
| Layer 2: source event identity | 这是 Provider 侧的哪条逻辑财报事件？ | `EarningsCalendarObservation.provider_event_id` 的受控语义 | Provider-native 或系统确定性生成 |
| Layer 3: canonical earnings event identity | 这是现实世界哪一次财报事件？ | `company_id + period_end_date + period_type`，派生为 `EarningsEvent.identity_key` | 系统 |

Layer 3 始终由系统拥有，保持不变。ADR-001、ADR-009、ADR-014 对 canonical identity 的决策
继续有效。

Layer 2 是 source lineage / replay / correction tracking 的身份，不进入 canonical
identity，也不决定 cross-provider reconciliation 的合并结果。

## 3. Decision

### 3.1 Provider-native ID 不再强制

`provider-native event ID required = NO`。

Provider 提供稳定 native ID 时，保留它作为 Layer 2 identity；Provider 不提供时，允许系统
从稳定业务事实生成确定性的 internal source identity。内部生成的 ID 不得伪装成上游 ID。

### 3.2 Layer 2 始终存在

`EarningsCalendarObservation` 必须有稳定的 source event identity。它可以是：

```text
native provider identity
或者
system-owned deterministic internal identity
```

不存在“没有 Layer 2 但继续 downstream”的状态。

### 3.3 物理字段兼容

当前 schema 的 `EarningsCalendarObservation.provider_event_id` 非空，并且已有：

```text
UNIQUE (raw_data_record, parser_version, provider_event_id)
INDEX  (source, provider_event_id)
```

本 ADR 将该字段定义为 **source event identity 的物理存储**：

```text
provider_event_id column
  = source_event_identity storage
```

这是保留现有 schema 的兼容解释，不是把内部生成的 ID 冒充为上游 ID。字段名本身是命名债；
未来若只做 rename，应单独迁移为 `source_event_identity`，不属于本 ADR 的实现范围。

本 ADR 不要求同时持久化 `provider_native_event_id` 与 `internal_source_identity` 两个独立
字段。现有 unique constraint、replay 与 reconciliation 只需要一个稳定的 source event
identity；native 与 internal 形式通过受控 namespace 区分。若未来产品需要同时保留
native evidence 和始终可比较的 internal identity，必须另开 schema ADR。

## 4. Provider-native ID

当 Provider 提供稳定、非空事件 ID 时：

- 它是 Layer 2 identity 的 provider-native 形式；
- 必须原样保留为 source lineage evidence；
- 不得进入 Layer 3 canonical identity；
- 同一 native ID 下的事实变化表示同一 Provider event 的 revision；
- 同一 native ID 指向不同 Company 或不同 fiscal period 时，必须写
  `collision` / `review_required`，不得猜选一方。

为避免与内部命名空间冲突，保留前缀：

```text
internal:
```

Provider-native ID 不得以 `internal:` 开头。若上游真实 ID 确实以该前缀开头，adapter 必须
显式转义为 `native:<value>`；无法在 255 字符限制内安全表示时 fail closed。

## 5. Internal Deterministic Source Identity

### 5.1 版本

```text
EARNINGS_SOURCE_EVENT_IDENTITY_VERSION = "earnings-source-event-identity-v1"
```

内部 identity 的存储形式：

```text
internal:v1:<64 位小写 SHA-256>
```

`v1` 与 hash payload 中的 version 必须一致。算法改变时必须使用新的 version 前缀和新的
payload version，不得原地改变旧 identity；旧 observation 保持 append-only。

### 5.2 必需输入

internal identity 的 canonical JSON 至少覆盖：

```text
source_event_identity_version
source_key
provider_key
issuer_identity
period_end_date
period_type
```

`source_key` 指 `DataSource.key` 的稳定业务字符串，不是 `DataSource.id` UUID 主键。

其中 `issuer_identity` 只允许以下两种 exact 形式之一：

```text
normalized CIK
或
normalized ticker + canonical exchange
```

规则：

- 有 CIK 时使用 CIK；
- 无 CIK 时，ticker 和 exchange 必须同时存在；
- 两者同时存在且互相冲突时 fail closed；
- `provider_symbol`、company name、provider 行位置不能替代 issuer identity；
- ticker-only、symbol-only、name-only 不允许生成 internal identity。

`period_end_date` 与 normalized `period_type` 必须同时存在。`fiscal_year`、
`fiscal_calendar_type`、`period_length_weeks` 是展示/兼容事实，不作为最小 identity 输入；
它们变化不得自动创建新 canonical event。

### 5.3 禁止输入

以下值 MUST NOT 作为 Layer 2 identity 输入：

```text
report_date / reportDate
estimated_release
release_session
date_confirmed
status
provider response order / raw_position
数据库 PK / created_at
wall clock
current monitoring pool state
parser_version
```

`parser_version` 继续作为 normalized revision 的 append-only 维度，不进入 source identity。

### 5.4 规范化与 hash

internal identity 使用 canonical JSON：

```text
internal:v1:
  SHA256(canonical_json({
    "source_event_identity_version": EARNINGS_SOURCE_EVENT_IDENTITY_VERSION,
    "source_key": ...,
    "provider_key": ...,
    "issuer_identity": {...},
    "period_end_date": "YYYY-MM-DD",
    "period_type": "Q1|Q2|Q3|FY|H1|H2|OTHER"
  }))
```

JSON 必须 sort keys、无空白、无 wall clock、无随机值。相同输入必须得到相同 identity。

## 6. Incomplete Input Behavior

| 情况 | 行为 |
|---|---|
| 有 Provider-native ID，period facts 不完整 | 允许创建 observation；candidate / reconciliation 按既有规则进入 UNMATCHED / review / fail-closed |
| 无 native ID，issuer identity 完整，period_end_date / period_type 完整 | 生成 internal identity，允许创建 observation |
| 无 native ID，issuer identity 不完整 | 保留 raw / parse lineage，不创建 observation、candidate 或自动 reconciliation |
| 无 native ID，period_end_date 或 period_type 缺失 | 保留 raw / parse lineage，不创建 observation、candidate 或自动 reconciliation |
| 有 native ID，但 native ID 映射到不同 Company / period | collision / review_required，不覆盖、不猜选 |

不得为了继续流程而伪造 Provider-native ID，也不得把 internal identity 命名为 Provider
原始 ID。

## 7. Source Identity 变化与冲突

### 7.1 Native ID 变化

Provider 改变 native ID 时：

- 新 ID 形成新的 Layer 2 identity；
- 旧 ID 的 observation 保持 append-only；
- 若 stable canonical facts 相同，后续 reconciliation 可映射到同一 canonical event；
- 若 canonical facts 冲突，进入 review，不自动建第二个 canonical event。

### 7.2 Internal identity 输入变化

Provider 无 native ID 时，若 `period_end_date`、`period_type` 或 issuer identity 变化：

- 旧 observation 保持；
- 新 observation 得到新的 internal identity；
- 系统 MUST 检查同一 `source_key + provider_key + issuer_identity` 下是否存在同一
  fiscal-period family 的既有 observation；
- 若 overlap 存在且 period identity 已变化，写 append-only
  `collision` / `review_required`，不得静默 fork 新 canonical event，也不得覆盖旧值；
- 若无法证明是不同 fiscal event，默认 fail closed 到 review。

Fiscal-period family 至少按已持久化的 `fiscal_year + period_type` 判断；缺失时按可用的
Provider fiscal label 判断。MUST NOT 使用 release date proximity 作为 correction 判断。

### 7.3 Ticker / exchange / CIK correction

issuer identity correction 形成新的 Layer 2 identity，但 Layer 3 canonical identity 仍由
Company + period identity 决定。旧 observation 不删除；新 identity 通过 reconciliation
映射或进入 review。

## 8. Replay / Idempotency

三层 identity 的 replay 角色保持分离：

| Replay layer | 使用的 identity |
|---|---|
| raw observation | `RawDataObservation(sync_run, raw_data_record)` + `RawDataRecord` unique tuple |
| source event | `(raw_data_record, parser_version, source_event_identity)` unique |
| canonical event | `EarningsEvent.identity_key` |

Offline replay 继续满足：

```text
network fetch = 0
只在 persisted RawDataRecord.payload 上重新解析
同 raw + parser + source identity 复用 observation
parser version 变化时 append-only 新 revision
provider/version provenance 保留
```

ADR-011 的 replay input digest 不依赖 Provider-native ID；它继续使用 raw manifest、provider
version、parser version 和 replay contract version。因此 ADR-011 无需改变。

## 9. Reconciliation Impact

ADR-014 继续有效：

- cross-provider reconciliation 仍以 `Company + period_end_date + period_type` 为 canonical
  grouping；
- Provider-native ID 与 internal source identity 都只作为 source lineage / conflict evidence；
- internal identity 不参与 fuzzy matching；
- 不新增 Provider trust ranking；
- 不扩大 destructive merge；
- loser、SourceEvidence、decision history 继续保留。

ADR-013 的 matching input revision 与 matching evidence 中引用 `provider_event_id` 的位置，
在本 ADR 下解释为 source event identity；matching strategy 本身不变。

## 10. Schema / Migration

```text
schema change required = NO
migration required = NO
```

理由：

- 现有 `provider_event_id` 已是非空字段，满足“Layer 2 始终存在”；
- 255 字符容量足以容纳 `internal:v1:` + 64 位 hex；
- 现有 unique constraint 与 index 继续表示 source identity 幂等；
- provider-native ID 与 internal ID 通过受控前缀区分，不需要新增字段。

命名债：

- 物理列名仍为 `provider_event_id`；
- 语义名称应为 `source_event_identity`；
- 纯 rename 是可选的未来迁移，不是本 ADR 的正确性前提；
- 若未来必须同时保存“可选 native ID”和“始终存在的 internal identity”为两个独立列，
  必须另开 schema ADR，而不是本次顺手扩展。

## 11. Zero-Cost Provider Compatibility

Provider 没有 native event ID 时，原则上可以进入 4.2F live contract，但必须同时满足：

```text
license / access gate 通过
+ 能提供 exact issuer identity
+ 能提供 period_end_date
+ 能提供 normalized period_type
```

这意味着：

- identity contract 不再因为“没有 Provider-native ID”而自动拒绝 Provider；
- Provider 仍然必须通过许可、保留、展示、衍生和 replay 权利检查；
- 只有 symbol + reportDate + fiscalDateEnding 的数据仍不足以完成 internal identity；
- Provider 若缺少 exchange / CIK / period_type，需要 adapter 或其他合法来源补齐，不能由
  generic domain service 猜测。

Alpha Vantage 不因本 ADR 自动获批。它当前 documented earnings calendar 只有 symbol、
reportDate、fiscalDateEnding、estimate、currency、timeOfTheDay，缺乏 generic contract
所需的 exchange/CIK 与 normalized period_type；是否可通过其他合法字段补齐属于后续
Provider / License Gate 评估。

## 12. 与 ADR-010 的关系

本 ADR **部分修订** ADR-010 第 2 节：

```text
provider_event_id MUST be supplied by upstream
```

被修订为：

```text
source event identity MUST exist
provider-native ID MAY be used when available
internal deterministic identity MAY be used when native ID is unavailable
```

ADR-010 的以下决策保持不变：

- canonical identity 与 source identity 分离；
- exact-only automatic reconciliation；
- no destructive merge；
- manual authority；
- provider absence 不触发 cancel/delete；
- Provider / license gate 和 runtime boundary。

本 ADR 不修改 ADR-009、ADR-011、ADR-013、ADR-014 的 canonical、replay、matching 或
reconciliation 决策。

## 13. Gate Decision

```text
PASS — system-owned source event identity is implementable without schema change
```

理由：

- canonical identity 已经是 system-owned，不依赖 Provider ID；
- replay 的 source of truth 是 persisted raw evidence；
- observation 已有非空 source identity 字段和唯一约束；
- internal identity 可以用稳定 issuer + period facts 确定性生成；
- 不完整输入、identity 变化和 collision 可以 fail closed；
- 不需要 fuzzy matching、schema change 或 migration。

本 ADR 只解除 identity contract 阻塞，不批准 Provider，也不替代 4.2F license gate。

## 14. 仍待确认

- 是否在未来用纯 rename 将物理列改名为 `source_event_identity`；
- 是否在另一个 ADR 中讨论同时保存 native 与 internal identity 两个独立字段；
- Alpha Vantage 等 Provider 是否能在不新增 Provider 调用/许可风险的前提下补齐
  exchange/CIK 与 period_type。

这些事项不阻塞本 ADR 的 identity contract，也不得在本 ADR 中实现。
