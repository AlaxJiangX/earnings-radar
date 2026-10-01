# ADR-022：IR Confirmation、Field Authority、Cancellation 与 Observation Contract

- 状态：已接受（4.5B Contract Documentation；contract frozen to fixture-first / live blocked）
- 日期：2026-10-01
- 决策者：产品负责人（Contract Gate 结论：4.5B contract shape frozen；live Provider blocked）
- 影响阶段：4.5B IR Confirmation
- 评审基线：`origin/main`，commit `647956e3e5c38c7881caed6ebeb63d06f34a4fdf`
- 范围：只冻结 fixture-first / contract shape。本 ADR 不实现 IR Provider、model、migration、
  command、UI 或 live source。

> 状态说明：4.5B = **CONTRACT FROZEN / FIXTURE-FIRST ONLY / LIVE BLOCKED**。
> 4.5A Filing ↔ Earnings 关联契约见 ADR-021，并且不受本 ADR 的 live blocker 影响。

## 1. 背景与边界

Stage 4.5 的第二部分需要把公司 Investor Relations（IR）官方信息接入财报事件生命周期：

- 官方确认的正式发布日期；
- 明确的电话会通知；
- 明确的财报结果发布；
- 明确的取消；
- 字段级 authority 与冲突处理。

但 IR 来源高度异构：

- 每家公司可能使用不同的 IR 页面、feed 或供应商；
- robots、terms、自动化访问、缓存、保留、派生、公开展示和再分发权利都不同；
- 具体首批公司与来源尚未确定。

因此 4.5B 只冻结 contract shape。live Provider 在逐来源许可 checklist 通过前必须保持
`LIVE BLOCKED`。fixture-first 开发、schema 设计和测试可以使用人工合成数据。

本 ADR 不改变 ADR-010 的 4.2 third-party calendar authority，也不改变 ADR-008 的
status lifecycle；它只补充 IR 官方确认的字段权限、冲突、取消、observation 与 replay
契约。

## 2. Source scope

`RATIFIED DECISION`

4.5B v1 只允许：

```text
official company IR press-release / event pages
official IR feed (RSS / JSON)
explicitly licensed vendor API
```

MUST NOT：

```text
general web crawler
arbitrary search-engine scraping
multi-site discovery crawler
```

Initial company scope：

- 由 operator 维护的 explicit allowlist；
- 最多 50 家公司；
- 每家公司必须已有 `Company.investor_relations_url`；
- 只允许 approved official host 或 approved vendor；
- run 必须持久化 frozen scope，至少包含：

```text
scope_version
ordered company_ids
source_keys
canonical scope digest
```

- replay MUST NOT 重新评估 current allowlist；
- 具体公司清单和每家公司来源尚未决定，MUST NOT 在文档中伪造批准结论。

Allowlist 是配置/操作数据，不是自动发现结果。新增公司或来源必须经过新的 scope revision
和对应的 license 结论。

## 3. Per-source live gate

每个 IR 来源在进入 live 前，必须逐项记录：

```text
authentication
robots
terms
automated access permission
caching
retention
derived data
private display
public display
redistribution
rate limits
deletion obligations
reviewer
reviewed_at
final conclusion
```

判定规则：

- 每一项都有明确结论；
- 任意 unknown / ambiguous / stale 结论 → `LIVE BLOCKED`；
- 只有 approved 来源可以进入 production ingestion；
- fixture-first 不因 live blocked 而停止；
- 条款变化、部署范围变化或使用范围扩大时必须重新审查。

本 ADR 不记录任何真实公司的许可批准，也不把网页可访问等同于允许自动抓取、缓存或展示。

## 4. Future schema：InvestorRelationsObservation

`RATIFIED DECISION`

MUST NOT 复用 `EarningsCalendarObservation`。4.5B implementation 预期新增独立模型：

```text
InvestorRelationsObservation
```

职责：

- append-only normalized observation；
- 保存 raw lineage、来源身份、期间事实和 IR item facts；
- 支持 replay、conflict 和 authority decision；
- Provider、parser 或 sync 不得直接写 EarningsEvent。

预期字段：

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `source_id` | FK DataSource，PROTECT；必须与 RawDataRecord source 一致 |
| `raw_data_record_id` | FK RawDataRecord，PROTECT |
| `provider_key`, `provider_version`, `parser_version` | 来源与解析版本 |
| `source_event_identity` | 稳定 provider-native ID 或 `internal:ir:v1:<sha256>` |
| `raw_position` | 1-based 原始位置 |
| `company_id` | FK Company，PROTECT |
| `period_end_date` | date，非空 |
| `period_type` | Q1 / Q2 / Q3 / FY / H1 / H2 / OTHER，非空 |
| `item_type` | release_confirmation / results_release / call_notice / cancellation |
| `confirmed_release` | nullable，date 或精确 datetime + precision |
| `earnings_release` | nullable，date 或精确 datetime + precision |
| `conference_call` | nullable，date 或精确 datetime + precision |
| `release_session` | nullable / unknown |
| `cancellation` | nullable explicit structure |
| `source_observed_at` | timestamptz nullable |
| `confidence` | 可解释等级 |
| `created_at` | UTC |

预期约束：

- unique `(raw_data_record, parser_version, source_event_identity)`；
- append-only，不允许 update / delete；
- `source` 必须与 `RawDataRecord.source` 一致；
- `item_type` 决定哪些 facts 必须非空；
- 缺少 exact Company 与完整 period identity 时，只保留 raw lineage，不创建 observation；
- 不使用 canonical EarningsEvent identity 作为 observation identity。

该 schema 尚未实现；实际字段名、枚举字符串和迁移编号由 4.5B implementation 阶段按本 ADR
落盘，不得在 Contract Documentation 阶段创建。

## 5. Future schema：InvestorRelationsDecision

`RATIFIED DECISION`

4.5B implementation 预期新增 append-only authority/history 模型：

```text
InvestorRelationsDecision
```

职责：

- 保存 IR observation 到 EarningsEvent schedule / lifecycle 的 authority decision；
- 记录 conflict、absence、review 与 manual override；
- 从 append-only history 推导 authority，不新增可变 lock flag。

预期字段：

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `observation_id` | FK InvestorRelationsObservation，PROTECT |
| `target_event_id` | FK EarningsEvent nullable，PROTECT |
| `decision_type` | confirmed_schedule / updated_conference_call / released / cancelled / conflict / no_match / ignored |
| `status` | open / resolved / rejected |
| `covered_fields` | 受控字段集合 |
| `rule_version`, `match_factors`, `reason` | 规则、证据与原因 |
| `actor_user_id`, `sync_run_id` | 人工或系统上下文 |
| `request_id` | 稳定请求身份 |
| `decided_at` | UTC |
| `supersedes_id` | self-FK nullable，PROTECT |
| `decision_key` | deterministic unique |
| `created_at` | UTC |

契约语义：

- append-only，不允许 update / delete；
- 最新有效 decision 通过 `supersedes` 链查询；
- manual decision 优先于 IR automatic decision；
- 同一 request / decision_key 重放必须复用原 decision；
- 冲突不得静默覆盖；
- decision 不直接修改 EarningsEvent；所有 schedule / status 写入必须调用既有 earnings
  service。

该 schema 尚未实现，具体字段与约束必须在 4.5B implementation ADR / migration review 中
再次核对。

## 6. Raw-first ingestion pipeline

`RATIFIED DECISION`

未来 IR ingestion 必须遵守：

```text
Provider
→ RawDataRecord
→ RawDataObservation
→ parse
→ InvestorRelationsObservation
→ InvestorRelationsDecision
→ existing schedule / lifecycle service
```

规则：

- Provider 只返回安全结构化结果；
- Provider MUST NOT 创建 SyncRun、RawDataRecord、RawDataObservation、observation、
  decision 或 EarningsEvent；
- sync orchestration service 创建 SyncRun，并通过 `audit.services` 保存 raw 与 observation；
- parser 只做规范化，不匹配 Company、不决定 authority、不写领域表；
- decision 与领域写入必须使用短事务；
- HTTP / network MUST NOT 进入数据库事务。

### 6.1 IR Provider contract

`InvestorRelationsProvider` 必须复用现有 Provider 协议和 `investor_relations` capability：

- 稳定 `provider_key` / `provider_version` 与支持的 source scope；
- 显式 connection / read timeout、User-Agent、rate limit、有限 retry 和错误分类；
- 请求范围只包含 frozen allowlist 中的 company_ids / source_keys；
- 返回安全的结构化 raw response，不包含凭据、Authorization 或敏感 query；
- 不写数据库，不创建 SyncRun、RawDataRecord、RawDataObservation、observation、decision 或
  EarningsEvent；
- Provider-specific parsing 与 authority 分离；
- 真实 transport 只在 per-source live gate 通过后启用；fixture-first 使用 FakeTransport，
  普通 CI MUST NOT 访问真实 IR 网络。

## 7. IR authority matrix

`RATIFIED DECISION`

| Field / State | Third-party calendar | SEC | IR | Manual |
|---|---|---|---|---|
| estimated_release | AUTO existing | NO | only explicit tentative | YES |
| confirmed_release | NO | NO | AUTO official explicit | YES |
| earnings_release | NO | NO | AUTO explicit results release | YES |
| conference_call | NO | NO | AUTO explicit call notice | YES |
| release_session | AUTO existing | NO | REFINE when explicit | YES |
| scheduled_confirmed | NO | NO | AUTO official confirmation | YES |
| released | NO | NO | AUTO explicit results release | YES |
| cancelled | NO | NO | AUTO explicit cancellation | YES |
| has_release_filing | not applicable | 4.5A-derived | not applicable | confirm/reject link |
| has_periodic_filing | not applicable | 4.5A-derived | not applicable | confirm/reject link |

SEC Filing does NOT advance EarningsEvent lifecycle。IR authority 只适用于对应
item_type 和字段；同一 IR 来源不得因为提供了某个字段而获得其他字段的自动权限。

## 8. Confirmation、conflict 与 precision

`RATIFIED DECISION`

### 8.1 Confirmation

IR 官方确认必须同时满足：

- official IR source；
- explicit date；
- unique canonical EarningsEvent；
- verifiable period identity；
- source item 明确表达 confirmed release schedule。

满足时：

1. 通过既有 schedule service 写 `confirmed_release`；
2. 调用既有 `confirm_earnings_event`；
3. 两步在同一个 outer transaction；
4. date-only 保持 date-only，不伪造具体时间；
5. 不直接写 `EarningsEvent.confirmed_release_*` 字段。

### 8.2 Conference-call-only evidence

只提供 conference call 的证据：

- 只更新 `conference_call`；
- MUST NOT 更新 `confirmed_release`；
- MUST NOT 推进 `scheduled_confirmed`；
- 除非同一来源同时明确给出 release date，否则不得确认财报 schedule。

### 8.3 Conflict precedence

字段级优先级：

```text
manual > IR > SEC filing state > third-party calendar
```

该顺序只在 source 对相应字段有 authority 时适用。`has_release_filing` /
`has_periodic_filing` 由 4.5A 派生，不是 schedule conflict 字段。

Same-authority conflict：

```text
review_required
do not auto-select
```

### 8.4 Precision

- refinement（unknown → date / exact）可以自动应用；
- regression（exact → date / unknown）不能自动应用，必须 review 或 manual；
- date-only 不得伪装成 UTC midnight；
- 所有写入必须经过既有 schedule service，写 DataChange / EarningsDateChange / AuditRecord。

## 9. Absence 与 cancellation

`RATIFIED DECISION`

```text
provider absence != cancelled
```

IR missing：

- no cancel；
- no downgrade；
- no delete。

SEC no filing：

- no negative earnings fact；
- 不得推导 `earnings_release`、`confirmed_release` 或 status。

Cancellation authority：

Allowed：

```text
explicit official IR cancellation
manual explicit cancellation
```

Not allowed：

```text
SEC Filing
third-party calendar disappearance
conference call cancellation
```

Conference call cancellation != EarningsEvent cancellation。取消必须携带 affirmative
evidence，并通过 `cancel_earnings_event` 的显式 intent 参数；absence 不得触发 cancellation。

## 10. Identity 与 replay

`RATIFIED DECISION`

### 10.1 Source identity

如果 Provider 提供稳定 provider-native ID：

- 原样保留；
- provider-native ID MUST NOT 以 `internal:` 开头；系统 internal identity 使用
  `internal:ir:v1:` namespace 区分；
- 不得进入 EarningsEvent canonical identity。

否则只在同时存在 exact Company 与完整 period identity 时生成：

```text
internal:ir:v1:<sha256>
```

输入：

```text
source_key
company_id
period_end_date
period_type
item_type
```

排除：

```text
mutable event date
fetched_at
raw_position
parser_version
```

缺少 period facts：

```text
raw lineage only
no normalized observation
no authority write
```

### 10.2 Replay

IR replay 必须：

- zero network；
- 只读取 persisted raw；
- 使用 same frozen scope；
- 重新解析后得到同一 source event identity；
- decision 幂等；
- manual leaf 不能被 replay 或 automatic decision 覆盖；
- 不允许用 today's allowlist、current IR URL 或内存对象替换 persisted scope。

## 11. Implementation acceptance gates

4.5B implementation 至少覆盖：

- observation / decision schema 与 append-only 约束；
- source identity 与 missing period facts 的 fail-closed；
- official source authority；
- confirmed schedule 与 conference-call-only 的差异；
- same-authority conflict 的 review_required；
- absence / cancellation authority；
- replay zero network 与 manual leaf 保护；
- per-source license checklist；
- fixture-first 普通 CI，live smoke 单独运行；
- Provider 不写领域表、不创建 SyncRun / RawDataRecord / observation / decision。

## 12. Non-goals

4.5B Contract Documentation MUST NOT：

- 实现 IR Provider；
- 下载或保存真实 IR 页面；
- 创建 model、migration 或 command；
- 批准任何具体公司或来源；
- 修改 EarningsEvent canonical identity；
- 修改 4.2 third-party calendar 字段权限；
- 绕过 4.5A Filing-derived state。

## 13. Open product decisions

仍待产品负责人确认：

1. 实际 IR company allowlist；
2. 每家公司 approved official source / URL / feed / vendor；
3. 每个来源的 robots / terms / license / retention / display / rate-limit 结论；
4. 任何在 implementation 阶段发现的真正未决 contract item。

这些 open decisions 不阻塞 4.5A implementation。

## 14. References

- `docs/decisions/ADR-003-release-filing-classification.md`
- `docs/decisions/ADR-007-earnings-date-change-precision.md`
- `docs/decisions/ADR-008-earnings-status-lifecycle-cancellation.md`
- `docs/decisions/ADR-010-earnings-calendar-observation-and-reconciliation.md`
- `docs/decisions/ADR-021-filing-earnings-link-classification-matching-review-replay.md`
- `docs/data-model.md` §6.8、§6.9
- `docs/architecture.md` §4.6
- `docs/data-sources.md` §4.4
- `docs/development-roadmap.md` Stage 4.5B
