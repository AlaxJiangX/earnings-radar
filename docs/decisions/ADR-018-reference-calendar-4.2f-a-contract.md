# ADR-018：Stage 4.2F-A Reference Calendar Contract

- 状态：已接受（4.2F-A planning / contract gate；本轮不实现代码）
- 日期：2026-09-29
- 决策者：产品负责人
- 评估与起草：Codex（按 Stage 4.2F-A Planning Gate 执行）
- 评审基线：分支 `codex/4.2f-a-planning-gate`，`origin/main`
  `2b351cca85fa1e2bc1d0f866b6b35205986ce0c4`
- 影响阶段：4.2F-A；4.2F-B 与 ADR-015 / ADR-014 canonical 契约不变

## 1. 背景与 Gate 结论

ADR-017 已接受 TWO-LAYER REFERENCE + CANONICAL 产品模型，并给出 v1
read-time projection、`schema change = NO` 的方向。本轮 4.2F-A planning gate 对该方向
做了仓库核对与 contract freeze。

核对结果：

- reference row 停在 `RawDataRecord + RawDataObservation + RawDataParseAttempt` 是可行的；
- `SyncRun.scope` 是 JSONField、`job_type` 是自由字符串，reference run 可以使用独立
  job type 与独立 scope contract，无需 schema change；
- `SecurityListing` 的 company / ticker / exchange / effective 字段通过
  `companies.services.update_security_listing()` 拒绝原地修改，只能走
  `transition_security_listing()`，该 service 不变量支撑 reference replay 确定性；
- 现有 repository 没有 live HTTP transport（只有协议、`ProviderHttpClient` 与
  FakeTransport），属于 4.2F-A 实现依赖，不是本 planning gate 的阻塞；
- 目前没有任何 Mode A Provider 获批，Alpha Vantage Free 仍按 ADR-016 被阻塞。

```text
Gate result = PASS (planning / contract)
Implementation = NOT STARTED
```

## 2. Product Boundary

4.2F-A 只做：

```text
Zero Data Cost Reference Calendar
Full Monitoring Pool Visibility（reference / estimated）
```

4.2F-A 不做，且不得通过任何隐式路径触发：

```text
EarningsCalendarObservation
EarningsReconciliationDecision
EarningsEvent(candidate / canonical)
SourceEvidence
candidate matching / reconciliation / promotion
earnings.calendar_window canonical sync
canonical correction / backfill
```

Zero Data Cost 定义沿用 ADR-017，不因本 ADR 扩展：

> 正常长期运行下，为覆盖配置的 Monitoring Pool 所需财报日历数据访问、API 调用、
> 数据订阅或数据许可，不需要支付任何经常性费用。

```text
Zero Data Cost != Zero Total Cost
```

## 3. Mode A Provider / License Contract

4.2F-A 的 provider 必须通过 reference-only 的 Mode A checklist。判定针对
**实际部署范围**：当前 4.2F-A 只批准 private / internal、单实例且数据只对 API key
持有者可见的 V1。未来 public / commercial 部署必须重新审查，不得沿用本次结论。

| 权利 / 条件 | 4.2F-A 是否需要 | 判定要求 |
|---|---|---|
| permanent free access，无 subscription / per-request / licensing fee | 必需 | 免费层必须长期有效；trial、credits、sample-only、需要付费升级才能覆盖 full pool 一律不通过 |
| 免费额度足够日常 full-pool reference sync | 必需 | 按每天一次预算；额度不足即 FAIL |
| automated private / server-side access | 必需 | 条款必须允许服务器端计划访问；禁止自动化即 FAIL |
| raw payload 持久化（`RawDataRecord` append-only） | 必需 | 条款必须允许保留原始响应；禁止缓存 / 存储即 FAIL |
| historical raw retention / offline replay | 必需 | 必须允许长期保留并从 persisted raw 离线重放；强制定期删除或终止即删除且无法满足架构即 FAIL |
| derived / reference projection | 必需 | 必须允许从 raw 派生展示字段（symbol、日期、session） |
| private / internal display | 必需 | 只对当前私有实例可见；任何他人可访问需重新界定为新的使用范围 |
| attribution / linkback / delay label 义务 | 条件必需 | 条款要求时必须可实现并记录 |
| API key 存储、轮换、最小权限 | 必需 | 实施要求；不得把 key 写入日志、fixture 或仓库 |
| notification / alert use | 当前不需要 | 4.2F-A V1 不发送通知；未来 reference 通知需单独许可审查 |
| future commercial / public redistribution | 当前不需要 | 记录为 out of scope；未来部署需新 gate，不因未来需求拒绝当前清晰的私有用途 |
| canonical 专用权利（period_type、CIK、event ID 等） | 不需要 | 不得用 canonical Provider 标准误伤 reference-only 用途 |

判定规则：

```text
PASS
  所有必需权利都有明确的当前条款或书面确认，且证据记录了 URL、版本/日期、
  reviewer、reviewed_at 与部署范围；免费额度覆盖 full Monitoring Pool。

FAIL
  任一必需权利被条款禁止；免费额度不足；需要付费或升级才能长期覆盖 full pool；
  只依赖 trial / credits / sample-only symbols。

UNKNOWN / BLOCKED
  条款沉默、模糊、版本过旧或无法确认任一必需权利。沉默不等于 permission；
  UNKNOWN 一律不得批准 production ingestion。
```

Provider neutrality：本 contract 适用于任何通过 Mode A 的 provider。Alpha Vantage
仍只是候选，未获批；它只有在未来通过上述 checklist 后才能进入 4.2F-A。

## 4. Reference Parser Contract

建议实现身份（后续代码使用，本 ADR 只冻结语义）：

```text
REFERENCE_CALENDAR_PARSER_VERSION = "reference-earnings-calendar-parser-v1"
```

输入：

```text
raw payload bytes
provider_key / provider_version
parser_version
```

输出：按 raw 顺序读取的 reference rows，每行至少包含：

```text
raw_position（1-based，必填）
provider_symbol（必填，否则 row-level reject）
report_date（reference / estimated date，必填；缺失或不可解析时 row-level reject）
session（pre_market / post_market / unknown，映射规则版本化）
company_name（provider 提供时保留）
parser_version
```

reference parser MUST：

```text
deterministic：同 bytes + 同 parser_version = 同 row 序列
无 DB / 无网络 / 无 wall clock / 无随机值
保留 raw_position 追溯
允许缺 canonical facts
```

reference parser MUST NOT：

```text
输出或伪造 period_type
输出 provider_event_id / ADR-015 source identity
输出 canonical period identity
解析 Company ORM 对象、EarningsEvent 或其他领域状态
做 symbol -> listing 的解析（属于 projection selector）
```

语义：

```text
PAGE_ERROR
  encoding / header / 结构完全不可解析：该 payload 不产生 row；
  运行标记 FAILED 或与其他页组合为 PARTIAL，raw lineage 保留。

PARTIAL
  合法页中存在无效 row：合法 row 保留，无效 row 计数并记录安全原因；
  projection status = PARTIAL。

COMPLETE
  所有 row 通过校验。

EMPTY
  header / 结构合法但没有数据 row：合法空日历，不是错误。
```

## 5. Reference Selector / Projection Contract

```text
REFERENCE_PROJECTION_VERSION = "earnings-reference-projection-v1"
REFERENCE_CALENDAR_JOB_TYPE = "earnings.calendar_reference_window"
```

read-time projection pipeline：

```text
persisted reference SyncRun
  -> validate reference scope contract
  -> load RawDataObservation set -> RawDataRecord payloads
  -> deterministic reference parser（projection version 映射固定 parser version）
  -> frozen MonitoringPoolSnapshot filter（见 §6）
  -> ordered display-only result
```

输入：

```text
reference sync_run_id
projection_version（默认 v1）
```

输出：

```text
projection status: COMPLETE / PARTIAL / EMPTY / FAILED
rows: display-only reference rows
diagnostics: out_of_pool / ambiguous / invalid_row / duplicate counts
freshness: data_as_of、projected_at、fresh / stale / unavailable
```

ordering（deterministic）：

```text
report_date asc
-> session order（pre_market、post_market、unknown）
-> provider_symbol asc
-> raw_data_record_id asc
-> raw_position asc
```

MUST：

```text
只读，不写任何 canonical 表
不调用当前 selector 重新选 pool
不访问网络
projection 失败时 fail closed，不返回部分 canonical 结果
同一 raw + parser/projection version + frozen snapshot = 同一输出
```

## 6. Frozen Monitoring Pool Matching

匹配只使用 run scope 指向的 persisted frozen `MonitoringPoolSnapshot`：

```text
MonitoringPoolSnapshot(as_of, selector_version, pool_hash)
  -> MonitoringPoolMember.basis[].security_listing_id
  -> SecurityListing（basis listing）
  -> normalized provider_symbol == listing.ticker
  -> Company
```

规则：

```text
normalization：trim + uppercase（与 companies.services.normalize_ticker 一致）
no fuzzy / no name-only / no exchange guessing / no provider alias 推断
```

结果分类：

```text
0 matches                -> OUT_OF_POOL，排除出展示
1 exact Company match    -> MATCHED，展示
同一 Company 多个 listing -> 视为一个 Company，去重展示
跨多个 Company            -> AMBIGUOUS，fail closed 排除，只计数
listing 缺失 / company 不一致 / as-of 无效 -> fail closed 排除
```

投影可能因此对某些 ticker 覆盖不足；这是 exact-only 的接受代价，不得用模糊匹配弥补。

## 7. Reference Identity

投影行身份只用于稳定展示、replay 与同一 payload 内去重：

```text
ReferenceRowKey = (raw_data_record_id, parser_version, raw_position)
ReferenceProjectionRowKey = (projection_version, pool_snapshot_id, ReferenceRowKey)
```

MUST NOT：

```text
进入 EarningsCalendarObservation.provider_event_id
作为 ADR-015 source event identity
作为 ADR-014 reconciliation identity
写入任何 canonical 表
使用 internal: 或 native: 命名空间
```

reference identity 可以重新计算、可以随 parser/projection version 变化；它不是 durable
domain identity，也不进入 Layer 2 / Layer 3。

## 8. Display Semantics

可曝光字段：

```text
display symbol（provider symbol 或匹配后的 listing ticker）
company display name（本地 Company 或 provider name，标注参考来源）
estimated report date
session label（pre_market / post_market / unknown）
provider key / provider display name
data_as_of（raw fetched_at）
projected_at
freshness state（fresh / stale / unavailable）
reference-only status label
```

标签必须区分：

```text
"预计（第三方参考）" / reference / estimated
"数据时间" 与 "读取时间"
"可能过期" / stale
```

MUST NOT 暗示：

```text
confirmed earnings date
released status
canonical EarningsEvent existence
canonical period identity
```

PRD 前端状态 `预计` 继续适用；reference row 永远不显示为 confirmed / released /
cancelled。

## 9. Empty / Partial / Failure Semantics

```text
合法 zero-row response     -> EMPTY；显示空 reference 状态，不删除、不取消
provider 省略某公司        -> 最新投影中该行消失；不触发 cancellation / deletion / status mutation
payload 部分可解析         -> PARTIAL；只展示上一份 COMPLETE / EMPTY，并标注更新不完整
timeout / 429 / quota      -> 本次运行 FAILED；保留上一份完整投影，标注 stale / unavailable
symbol 无法解析             -> 从用户可见行排除，只保留 diagnostic 计数
```

UI 规则：

```text
latest COMPLETE / EMPTY  -> 展示该投影 + 新鲜度
latest PARTIAL           -> 展示最近 COMPLETE / EMPTY + "更新不完整" 标记
latest FAILED/UNAVAILABLE-> 展示最近 COMPLETE / EMPTY + "可能过期/暂不可用" 标记
若没有历史完整投影        -> 明确不可用状态，不编造行
```

任何失败都 MUST NOT 修改 canonical EarningsEvent、candidate、decision 或通知状态。

## 10. Retention / Replay Contract

reference replay 定义：

```text
network fetch = 0
只在 persisted RawDataRecord.payload 上重新执行 reference parser + projection
```

输入定位：

```text
reference SyncRun -> scope（provider / window / monitoring pool as-of / hash / selector version）
-> RawDataObservation -> RawDataRecord payloads
projection_version -> parser_version（固定映射）
pool scope -> MonitoringPoolSnapshot -> basis listings
```

确定性依赖：

- raw payload 未被改写（append-only）；
- `SecurityListing` 身份字段通过 service 不变量保持不可变（`update_security_listing`
  拒绝 company / ticker / exchange / effective 字段，只能 transition）；
- snapshot / member 通过 append-only 保持不可变。

已识别 gap：`SecurityListing` 的身份不可变性是 service 级合同，不是 DB 级触发器。
4.2F-A v1 依赖该合同并必须用测试锁定；若未来要求 DB 级防篡改或逐行 resolution 证据，
需要另开 ADR / schema 决策。

reference replay 不创建新的 `SyncRun`、`RawDataObservation` 或 parse attempt；它是纯读
投影。若未来需要持久 replay artifact，另开 ADR，不得复用 ADR-011 canonical replay
语义。

## 11. Window Contract

4.2F-A 采用 forward-only reference window：

```text
forward_horizon_days = 90（可配置）
past_correction_days = 不适用
```

理由：reference layer 只提供未来可见性；provider absence 不表示 correction，
不触发 cancellation，也不参与 canonical backfill。ADR-010 的 canonical 90 forward +
30 correction 窗口仍只属于 4.2F-B，不因本 ADR 改变。

reference run scope 必须独立于 `earnings.calendar_window` 的 canonical scope，并记录
window_start / window_end、provider / pool contract 与 projection/parser version。

## 12. Freshness Contract

```text
data_as_of = 投影中 raw payload 的最早 fetched_at（保守取最旧）
projected_at = 读取时刻
stale threshold = REFERENCE_CALENDAR_STALE_AFTER_HOURS（可配置）
default = 48 小时（每日同步的 2 倍，工程默认值，不是 SLA）
```

状态：

```text
fresh        = now - data_as_of <= threshold
stale        = now - data_as_of > threshold
unavailable  = 没有可用的 COMPLETE / EMPTY 投影
```

provider absence / 空日历不得被判为 stale 或系统错误；只按 data_as_of 判断。

## 13. Schema / Migration Decision

```text
schema change required = NO
migration required = NO
```

依据：raw / parse attempt / SyncRun scope / snapshot / member / listing 已能承载
read-time projection 所需的全部持久化事实。若未来需要持久 reference history、变更追踪
或逐行 resolution evidence，必须另开 ADR。

## 14. Runtime Boundary

4.2F-A 未来实现可以包含：

```text
Mode A reference provider adapter（复用 providers.Provider / ProviderHttpClient；
需要新增 live HTTP transport）
reference scope / idempotency / run ownership（独立 job type）
raw persistence（复用 audit services）
reference parser
reference projection selector
frozen pool filter
read-only service / endpoint / template wiring
freshness metadata
reference-only sync entry point
tests / docs
```

4.2F-B 专属，4.2F-A MUST NOT 触碰：

```text
EarningsCalendarObservation / candidate / decision / promotion
earnings.calendar_window canonical sync
canonical correction / backfill
canonical notification
```

## 15. Test Matrix

未来 implementation + verification 至少覆盖：

```text
parser determinism / row validation / session mapping / duplicate dedupe
projection ordering / projection identity / replay determinism（network fetch = 0）
frozen pool stability；same payload + snapshot + version = same output
ticker exact match；same-Company multi-listing；cross-Company ambiguity fail closed
0-match exclusion；alias 未批准时拒绝
empty vs malformed payload；partial row 计数
provider absence 不修改 domain；stale / fresh 标记
no EarningsCalendarObservation / EarningsEvent / decision / SourceEvidence writes
assert canonical / domain 表计数在投影前后不变
PostgreSQL integration：run scope / ownership / raw idempotency / snapshot reuse /
replay with frozen listing identifiers
普通 CI 继续阻断真实 HTTP；smoke test 单独运行
```

纯 parser / projection 逻辑用 pytest 单元测试；run scope、snapshot、并发与 replay 相关
路径使用 PostgreSQL integration tests。

## 16. Open Gaps / Risks

- 没有 Mode A Provider 获批；Alpha Vantage Free 仍 blocked，下一步应先做 focused
  Mode A license gate；
- 当前没有 live HTTP transport，属于实现前置；
- `SecurityListing` service 级不可变性未由 DB 强制，属于 replay 确定性的已知依赖；
- 跨 Company ticker 歧义会降低 reference 覆盖，按 exact-only 接受；
- raw payload 受 1 MiB 限制，provider 超限必须 fail closed。

## 17. 本轮不做

- 不实现 provider adapter、reference parser、selector、command、endpoint、UI、schedule；
- 不新增 model、migration 或 DB 约束；
- 不批准 Alpha Vantage 或其他 provider；
- 不放宽 ADR-015 / ADR-014 canonical identity；
- 不改变 canonical 90 天 forward + 30 天 correction 窗口。

## 参考

- `docs/decisions/ADR-015-system-owned-source-event-identity.md`
- `docs/decisions/ADR-016-alpha-vantage-free-provider-gate.md`
- `docs/decisions/ADR-017-zero-data-cost-reference-calendar.md`
- `docs/architecture.md` §4.5
- `docs/data-sources.md` §3、§8
- `docs/development-roadmap.md` Stage 4.2F
- `docs/product-requirements.md` §7.1、§12.2
