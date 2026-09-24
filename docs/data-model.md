# Earnings Radar 数据模型

> 状态：逻辑数据模型规划稿，不是 Django model 或迁移代码。
>
> 约定：主键建议使用 UUID；所有带时刻的字段使用 PostgreSQL `timestamptz` 并以 UTC 写入；只有自然日语义的字段使用 `date`。

## 1. 建模原则

1. 公司身份与可变化的股票代码分开；CIK 优先标识发行人。
2. 当前状态与历史证据分开；关键变更不可只覆盖旧值。
3. 原始数据、标准化观察值和领域当前值分层保存。
4. Provider 只能产出原始记录和标准化输入，领域服务统一落库。
5. 业务唯一键加数据库唯一约束，所有同步以 upsert/比较后写入方式实现幂等。
6. 历史业务对象优先停用、关闭有效期或取消，不物理删除。
7. 来源证据能回答：谁、何时、通过哪个任务、从哪个 URL 获取了什么，以及为何形成当前值。

## 2. 关系总览

```text
User 1---* WatchlistItem *---1 Company 1---* SecurityListing 1---* IndexMembership *---1 Index
  |              |                    |             |
  |              *---* ReminderRule   |             +-- ticker / exchange / share class history
  |                                   |
  +---* ReminderRule                  +---* EarningsEvent 1---* EarningsDateChange *---1 DataChange
  +---* Notification                  |          |
                                      |          *---* Filing (via FilingEarningsLink)
  |
  +---* IndexChangeLeg *---1 IndexChangeEvent

DataSource 1---* RawDataRecord *---1 SyncRun (first_sync_run)
                     |
                     +---* RawDataObservation *---1 SyncRun
                     +---* SourceEvidence ---* domain records

User / SyncRun / SourceEvidence 1---* DataChange
User / SyncRun 1---* AuditRecord
```

## 3. 用户与偏好

### 3.1 `User`

建议从项目创建时即使用自定义 Django User，避免后期更换用户模型。

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | UUID PK | 用户主键 |
| `email` | `citext` 或规范化字符串，unique | 登录和邮件地址 |
| `password` | Django 管理 | 不自行存明文或可逆密文 |
| `is_active`, `is_staff`, `is_superuser` | boolean | Django 权限 |
| `timezone` | IANA timezone，默认待确认 | 仅用于展示和提醒计算 |
| `preferred_language` | enum/string | 默认语言待确认 |
| `email_verified_at` | timestamptz nullable | 若开放注册则需要 |
| `created_at`, `updated_at`, `last_login` | timestamptz | UTC |

约束与索引：规范化后的 email 唯一；时区必须可由 IANA 数据库解析。

### 3.2 `UserNotificationPreference`

保存用户级默认开关，避免把大量布尔字段塞进 User。

| 字段 | 说明 |
|---|---|
| `user_id` | one-to-one User |
| `email_enabled`, `in_app_enabled` | 渠道总开关 |
| `default_lead_time` | MVP 默认 1 天，准确语义待确认 |
| `digest_time_local`, `digest_weekday` | 摘要偏好；默认值待确认 |
| `created_at`, `updated_at` | UTC |

## 4. 公司、股票代码与 CIK

### 4.1 `Company`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | UUID PK | 公司主记录 |
| `legal_name`, `display_name` | string | 法定/展示名称 |
| `cik` | 10 位规范化字符串，unique nullable | 前导零保留；未匹配时可暂空 |
| `country_code` | ISO code nullable | 国家/地区 |
| `issuer_type` | enum | domestic / foreign_private / other / unknown |
| `fiscal_year_end_month_day` | string/date fragment nullable | 不只存月份，避免 52/53 周公司误解 |
| `investor_relations_url` | URL nullable | 当前 IR 入口 |
| `monitoring_status` | enum | active / inactive / pending_identity |
| `monitoring_recalculated_at` | timestamptz | 派生状态更新时间 |
| `created_at`, `updated_at` | timestamptz | UTC |

CIK 为空的公司不能与 SEC 文件做确定性匹配。CIK 后续合并/修正必须写审计，不能直接制造第二条公司。

阶段 2.3 已实现：Service 将输入 CIK 规范化为 10 位 ASCII 数字并保留前导零；非空 CIK 由数据库唯一约束保护。暂时无 CIK 的创建必须提供预分配 UUID，避免以名称误合并。相同 CIK 但字段不同的重复写入会拒绝并要求走带审计的更新流程；Company 主数据的跨来源优先级、人工锁定与自动覆盖策略仍待产品负责人确认，真实 Provider 接入前不得自行推断。财报日历字段的 4.2 authority 已由 ADR-010 单独确定，不改变 Company 主数据规则。

### 4.2 `SecurityListing`

代表一个公司在某交易所、某有效期内使用的股票代码。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `ticker` | 规范化代码 |
| `exchange` | 规范化交易所代码 |
| `security_name`, `security_type` | 证券名称/类型 |
| `share_class` | A/B/C 类或其他 share class nullable |
| `is_primary` | 当前是否为主要展示代码 |
| `effective_from`, `effective_to` | date；有效期半开区间 |
| `source_evidence_id` | 当前关系的主要来源证据 |
| `created_at`, `updated_at` | UTC |

约束：同一交易所和 ticker 的有效期不能重叠；同一公司同一时点可以有多个 listing/share class，但最多一个默认展示代码。阶段 2.3 以 PostgreSQL `daterange` 半开区间排他约束（并启用 `btree_gist`）落实这两个规则，历史 ticker 通过设置 `effective_to` 保留而不覆盖。区间统一为 `[effective_from, effective_to)`：切换 ticker 或交易所时，Service 在一个事务中把旧 listing 的 `effective_to` 设为切换日，并创建从该日开始的新 UUID listing；不得通过通用更新入口改写 company、ticker、exchange、effective_from 或 effective_to。创建追加 AuditRecord，旧区间关闭写对应 DataChange，后继记录仅写创建 AuditRecord；Admin 只读。切换的幂等成功还必须精确核对旧 listing 的 `effective_to` DataChange（包括使用与创建时相同公共规则重算的 `change_key`）、旧 listing 关闭 AuditRecord 和后继创建 AuditRecord；即使字段内容正确，只要 `change_key` 不一致也视为审计损坏，拒绝成功并要求人工核查，绝不自动改写或补造历史。搜索索引覆盖 ticker 和公司名称。URL 使用 ticker 时应处理历史代码与歧义；长期建议内部 canonical URL 使用稳定公司 ID，但是否改变 PRD 路由待确认。

## 5. 指数及成分关系

### 5.1 `MarketIndex`（3.1A 已实现；provider_symbol 延后至 3.2）

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `code` | unique，如 `SP500`, `NASDAQ100`, `DJIA`, `RUSSELL2000` |
| `name` | 展示名称 |
| `provider_symbol` | Provider 使用的代码 nullable |
| `index_group` | LARGE（S&P 500/Nasdaq 100/Dow 30）或 SMALL（Russell 2000） |
| `is_enabled` | 是否纳入监控池 |
| `created_at`, `updated_at` | UTC |

### 5.2 `IndexMembership`

保存当前和历史指数成分关系。阶段 3.1B 已实现。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `index_id`, `security_listing_id` | FK；成员身份落到具体证券 |
| `effective_from` | date |
| `effective_to` | date nullable；半开区间结束 |
| `announcement_date` | date nullable |
| `status` | announced / active / ended / cancelled / corrected |
| `supersedes` | OneToOneField self (PROTECT)；替代记录指向旧记录，形成修订链表 |
| `last_verified_at` | timestamptz nullable |
| `source_evidence_id` | 当前关系的主要证据 |
| `created_at`, `updated_at` | UTC |

约束：
- 同一 SecurityListing + Index 的规范生效区间不得重叠（ExclusionConstraint，仅规范状态）
- 同一 index + security_listing + effective_from 最多一条规范记录（UniqueConstraint，仅规范状态）
- effective_to > effective_from（ended 必须有 effective_to）
- announcement_date ≤ effective_from
- membership 有效期必须包含在 listing 有效期内（membership 侧 + listing 侧 deferred constraint trigger）
- corrected/cancelled 不参与任何规范约束

修订关系：`replacement.supersedes → old`，`old.superseded_by → replacement`。Correction chain 为链表 M1←M2←M3。

Selector：`NORMATIVE_STATUSES = (announced, active, ended)`。as-of 查询包含 `effective_from ≤ date < effective_to`。`current_listing_indexes_as_of()` 和 `company_indexes_as_of()` 支持 `is_enabled` 过滤（默认仅启用指数）。

Provenance：自动来源需 SyncRun + SourceEvidence（通过 `resolve_source_evidence_reference`）；人工来源需 actor_user + reason + request_id。新建只写 CREATE AuditRecord，不写初始 DataChange。

修正规则：`correct_membership` 将原记录设为 `corrected` 并创建带有 `supersedes` 的后继记录。修正的旧记录 AuditRecord `before` 为完整快照（`_serialize_membership`，包含 index_id、security_listing_id、status、effective_from、effective_to、announcement_date、last_verified_at、source_evidence_id、supersedes_id），`after` 记录 corrected 状态和保留的 effective_to。仅 `source_evidence` 发生变化（所有身份和元数据字段不变）时允许，新证据通过顶层 `source_evidence` 参数传入而非 `replacement_values`；无新证据时后继 `source_evidence` 为 None。

关闭规则：`close_memberships_for_listing` 缩短 listing 有效期时：
- `effective_from >= new_effective_to` 的未来 announced 成员关系取消；
- `effective_from < new_effective_to` 的 ended 成员关系（`old.effective_to > new_effective_to`）不直接修改，而是将旧记录标记为 corrected 并创建具有缩短后 `effective_to` 的后继记录（同上 AuditRecord 快照规则）；
- `effective_from < new_effective_to` 且 `old.effective_to <= new_effective_to` 的 ended 成员关系跳过不处理（listing 延长关闭不能延长已退出的指数成员关系）；
- announced/active 成员关系直接缩短。action 标签反映最终实际状态：cancelled、corrected、skipped、announced、active 或 ended。

Company 不直接拥有 IndexMembership。公司级指数归属由其全部有效 SecurityListing 的有效成员关系去重聚合：任一 listing 属于某启用指数，公司即显示属于该指数；多个 share class 同属一个指数时，底层保留多条 membership，Company 页面只聚合展示。历史 ticker 对应的旧 listing 和 membership 通过有效期保留，不改写为当前 ticker。

公司监控池按公司聚合计算：`存在任一有效 listing 的启用指数 membership OR 存在任一有效 WatchlistItem`。因此某个 share class 被移除不等于公司退出监控池；必须检查公司其他 listing 和用户自选股。详见 ADR-002。

### 5.3 `IndexChangeEvent`

面向业务展示和通知的变化聚合。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `movement_direction` | UPGRADE / DOWNGRADE / CROSS_INDEX / NONE |
| `monitoring_impact` | CONTINUES / ENTERS_BASE_POOL / EXITS_BASE_POOL / REENTERS_BASE_POOL |
| `announcement_date`, `effective_date` | date nullable |
| `status` | announced / upcoming / effective / cancelled / corrected |
| `previous_state`, `new_state` | JSON 快照，仅作审计/展示，不替代关系表 |
| `aggregation_key` | unique stable key |
| `detected_at` | timestamptz |
| `source_evidence_id` | 主要证据 |
| `created_at`, `updated_at` | UTC |

### 5.4 `IndexChangeLeg`

保存聚合事件下的原子变化，使“Russell 2000 移除 + S&P 500 加入”既能作为一条偏移展示，又不丢失证券级事实。

| 字段 | 说明 |
|---|---|
| `event_id` | FK IndexChangeEvent |
| `index_id` | FK MarketIndex |
| `security_listing_id` | FK SecurityListing |
| `membership_id` | FK IndexMembership nullable |
| `action` | ADDED / REMOVED；唯一的原子动作维度 |
| `announcement_date`, `effective_date` | date nullable |
| `source_evidence_id` | 来源证据 |

唯一建议：`event + index + security_listing + action + effective_date`。同一公司、同一 effective_date 的加入和移除自动聚合；日期相差 1–7 个自然日进入待复核候选，超过 7 日保持独立。

`movement_direction` 与 `monitoring_impact` 彼此独立：Russell 2000 → LARGE 为 UPGRADE，LARGE → Russell 2000 为 DOWNGRADE，S&P 500/Nasdaq 100/Dow 30 内部变化为 CROSS_INDEX，其他为 NONE。`ENTERS_BASE_POOL` 仅用于历史上从未进入过基础池的首次进入；`REENTERS_BASE_POOL` 仅用于历史上退出后重新进入。修正/取消使用 IndexChangeEvent.status 和变更历史表达，不伪装成原子动作。详见 ADR-002。

## 6. 财报事件与日期变化

### 6.1 `EarningsEvent`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `fiscal_year` | integer |
| `period_type` | Q1 / Q2 / Q3 / FY / H1 / H2 / OTHER；年度统一为 FY |
| `period_end_date` | date nullable |
| `includes_q4` | boolean；FY 固定为 true，其他期间默认 false |
| `fiscal_calendar_type` | MONTH_BASED / WEEK_BASED_52_53 / OTHER |
| `period_length_weeks` | integer nullable；周制年度通常为 52 或 53 |
| `identity_key` | 正式事件稳定唯一键；候选事件为空 |
| `identity_rule_version` | 生成 identity_key 的规则版本 |
| `identity_status` | CANDIDATE / CANONICAL |
| `estimated_release_at` | timestamptz nullable；仅 `estimated_release_precision=exact_datetime` 时非空 |
| `estimated_release_date` | date nullable；仅 `estimated_release_precision=date_only` 时非空 |
| `estimated_release_precision` | unknown / date_only / exact_datetime；非空，默认 unknown |
| `confirmed_release_at` | timestamptz nullable；仅 `confirmed_release_precision=exact_datetime` 时非空 |
| `confirmed_release_date` | date nullable；仅 `confirmed_release_precision=date_only` 时非空 |
| `confirmed_release_precision` | unknown / date_only / exact_datetime；非空，默认 unknown |
| `earnings_release_at` | timestamptz nullable；仅 `earnings_release_precision=exact_datetime` 时非空 |
| `earnings_release_date` | date nullable；仅 `earnings_release_precision=date_only` 时非空 |
| `earnings_release_precision` | unknown / date_only / exact_datetime；非空，默认 unknown |
| `conference_call_at` | timestamptz nullable；仅 `conference_call_precision=exact_datetime` 时非空 |
| `conference_call_date` | date nullable；仅 `conference_call_precision=date_only` 时非空 |
| `conference_call_precision` | unknown / date_only / exact_datetime；非空，默认 unknown |
| `release_session` | pre_market / after_market / during_market / unknown；非空，默认 unknown |
| `status` | SCHEDULED_ESTIMATED / SCHEDULED_CONFIRMED / RELEASED / CANCELLED |
| `confidence` | 可解释等级或数值，算法待确认 |
| `primary_source_evidence_id` | 当前主证据 |
| `created_at`, `updated_at` | UTC |

正式唯一身份已确定为 `company_id + period_end_date + period_type`，并由带版本的规范化函数生成 `identity_key`。年度财报统一为 `FY + includes_q4=true`，上游 Q4 年度标签不另建事件。52/53 周通过 `fiscal_calendar_type` 和 `period_length_weeks` 表达，不作为 period_type。`fiscal_year` 是来源/展示属性，不参与唯一键。数据库对非空 `identity_key` 设置唯一约束，并要求 CANONICAL 事件必须有 `period_end_date`、`period_type`、`identity_key` 和 `identity_rule_version`。

当 `period_end_date` 未知时，只能创建 CANDIDATE 事件：它依赖 Provider 的外部事件标识和来源证据去重，不能使用 `company + fiscal_year + period_type` 作为正式身份。4.1D 通过 ADR-009 的 promotion 在同一个 EarningsEvent row 上补齐 canonical identity facts（`period_end_date`、`period_type`、派生的 `includes_q4` / `identity_key` / `identity_rule_version`），将 `identity_status` 原子变为 canonical。Promotion 是 completion 而非 correction：不修改 `company` 或 fiscal metadata，不改变 status、schedule 和既有历史；已有不同的 `period_end_date` / `period_type` 或 existing canonical collision 时 fail closed，不做 candidate dedup、merge/split 或自动合并。每个真实 identity 字段变化写 DataChange，一次 promotion 写一条 operation-level AuditRecord；`EarningsEvent.source_evidence` 保持原值。跨 Provider 的候选去重、合并与拆分属于 4.2，其 contract 已由 ADR-010 冻结：external ID 仅存在于 observation / reconciliation 层，V1 只做 exact-only automatic match，不做 destructive merge，canonical collision 写 decision 并保留 loser。详细决策见 ADR-001、ADR-007、ADR-009 与 ADR-010。

EarningsEvent.status 只回答“财报安排/发布到了哪一步”，不回答 SEC 文件是否提交。正常 transition matrix、terminal semantics、correction 和 reinstatement 见 ADR-008。`cancelled` 表示整个 logical EarningsEvent 被明确取消或证实不成立，不表示电话会取消或普通日期变化；Provider absence 不能触发 cancellation。

同一 canonical earnings identity 的真实取消后重新安排复用原 EarningsEvent，通过显式 reinstatement 恢复，不创建第二个 canonical event。若 event identity 本身不确定或错误，4.1C fail closed，交由 4.1D/4.2 处理。Status history 使用 `DataChange(field_name="status")` 与 AuditRecord，不新增 `EarningsStatusChange`。

四个发布时间字段只允许以下三种 current-state 表示，不能使用模糊双真值：

```text
unknown:           *_at = NULL, *_date = NULL, *_precision = unknown
date_only:         *_at = NULL, *_date = YYYY-MM-DD, *_precision = date_only
exact_datetime:    *_at = timestamp, *_date = NULL, *_precision = exact_datetime
```

四个字段都允许 date-only，因为预计、确认、实际发布和电话会来源都可能暂时只有自然日。exact datetime 以时区感知 UTC 保存；date-only 保持 `date`，不得转换成 UTC midnight。`release_session` 独立于 precision，使用领域枚举中的 `unknown` 表达未知，不使用 NULL。数据库必须为每组 `*_precision`、`*_at`、`*_date` 建立 CheckConstraint，Service 是唯一写入入口。

### 6.2 `EarningsDateChange`

EarningsDateChange 是四个发布时间字段和 `release_session` 的 append-only 领域历史，不是 current state，也不是 status transition history。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `earnings_event_id` | FK EarningsEvent，PROTECT |
| `field_name` | estimated_release / confirmed_release / earnings_release / conference_call / release_session |
| `change_kind` | value_change / precision_refinement / precision_regression |
| `old_precision`, `new_precision` | unknown / date_only / exact_datetime / session_only |
| `old_date`, `new_date` | date nullable；date_only 历史值 |
| `old_datetime`, `new_datetime` | timestamptz nullable；exact_datetime 历史值 |
| `old_session`, `new_session` | nullable；release_session 历史值 |
| `data_change_id` | OneToOne DataChange，PROTECT，unique |
| `detected_at` | timestamptz |
| `created_at` | UTC |

只有一个受控字段的规范化 value 或 precision 发生非 no-op 变化时创建记录。`value_change` 表示业务日期、exact datetime 的具体时刻或具体 session 实际变化；`precision_refinement` 表示相同业务日期下精度提升或 unknown 升级；`precision_regression` 表示 precision 或信息质量下降。三者都同时创建 DataChange 和 EarningsDateChange；通知策略独立于历史记录。

canonical 字段名为 `estimated_release`、`confirmed_release`、`earnings_release`、`conference_call` 和 `release_session`。DataChange 与 SourceEvidence 使用这些逻辑字段名，`*_at` / `*_date` / `*_precision` 只是表示列。

DataChange 的 canonical JSON 只允许：

```text
null
{"kind":"date","value":"2026-10-24","precision":"date_only"}
{"kind":"datetime","value":"2026-10-24T20:30:00Z","precision":"exact_datetime"}
{"kind":"session","value":"after_market","precision":"session_only"}
```

对于 `release_session`，`old_session/new_session` 必须非空，`old_precision/new_precision` 固定为 `session_only`。数据库负责阻止 precision 与 date/datetime/session 表示冲突；`change_kind` 的语义分类由 Service 和 rule version 负责。

EarningsDateChange 不保存 `old_status/new_status`、`event_status_at_change`、`is_official`、直接 `source_evidence_id` 或第二份 `change_key`。历史 provenance 使用真实的 `DataChange.source_evidence_id` 关系；幂等依据保留在 DataChange 的唯一 `change_key`。

建议索引：`(earnings_event, detected_at)`、`(field_name, change_kind, detected_at)`。EarningsDateChange 在模型和 Admin 中均为 append-only，不允许 update/delete。

完整边界、precision 和事务规则见 `docs/decisions/ADR-007-earnings-date-change-precision.md`。

### 6.3 `EarningsCalendarObservation`（4.2B 已实现）

> 本节描述已进入 main 的 4.2B schema foundation。本表当前可由 observation persistence
> primitive 写入；normalized ingestion / parser / replay workflow 仍属于 4.2C。

provider-neutral normalized revision，保存 provider external identity 与 raw lineage，支撑
replay 与 reconciliation。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `source_id` | FK DataSource，PROTECT；必须与 RawDataRecord source 一致 |
| `raw_data_record_id` | FK RawDataRecord，PROTECT |
| `provider_key`, `provider_version`, `parser_version` | 来源与解析版本 |
| `provider_event_id` | 非空 stable source identity；不进入 EarningsEvent identity |
| `raw_position` | 原始页内 1-based 记录位置 |
| company hints | `cik` / `ticker` / `exchange` / `provider_symbol` / `company_name` |
| fiscal facts | `fiscal_label_raw` / `fiscal_year` / `period_end_date` nullable / normalized `period_type` nullable / `fiscal_calendar_type` / `period_length_weeks` |
| schedule facts | `estimated_release_date` / `estimated_release_at` / `estimated_release_precision` / `release_session` |
| `confidence`, `source_observed_at`, `created_at` | 可追溯信息 |

约束与查询：

- UNIQUE `(raw_data_record, parser_version, provider_event_id)`；
- index `(source, provider_event_id)`；
- append-only：不允许 update / delete；
- primitive 校验 `source` 与 `RawDataRecord.source` 一致，且 `source.provider_adapter == provider_key`；
- 缺失 stable provider_event_id 的记录 MUST NOT 写入本表；
- 本表不决定 canonical identity，也不直接写 EarningsEvent。

### 6.4 `EarningsReconciliationDecision`（4.2B 已实现）

> 本节描述已进入 main 的 4.2B schema foundation。本表当前可由 decision persistence
> primitive 写入；decision creation policy、matching、conflict / review 与 manual authority
> workflow 仍属于 4.2D-4.2E。

append-only decision history，结构化保存 review / collision / mapping / dedup / conflict 事实。
结构化 match factors MUST NOT 被塞进 AuditRecord JSON。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `observation_id` | FK EarningsCalendarObservation，PROTECT |
| `decision_type` | `created_candidate` / `matched_candidate` / `matched_canonical` / `duplicate_of` / `collision` / `conflict` / `review_required` / `no_match` / `ignored` |
| `status` | `open` / `resolved` / `rejected`；supersession 通过 `supersedes` 链表达，不使用 `superseded` status |
| `target_event_id` | FK EarningsEvent nullable，PROTECT |
| `covered_fields` | manual authority decision 覆盖的字段集合；仅在 resolved manual decision 上可非空 |
| `rule_version`, `match_factors`, `reason` | 规则、证据与原因 |
| `actor_user_id`, `sync_run_id` | 人工或系统上下文 |
| `decided_at` | UTC |
| `supersedes_id` | self FK nullable，PROTECT，形成 append-only decision chain |
| `decision_key` | deterministic unique |

契约语义：

- 旧 decision MUST NOT update / delete；新 decision 通过 `supersedes` 指针表达替代；
- "最新有效 decision" MUST 可查询、可重放、可审计；
- `decision_key` 为 deterministic unique，覆盖 observation、decision_type、status、
  target_event、covered_fields、match_factors、rule_version、supersedes 与 actor / request
  identity；MUST NOT 包含 `decided_at`，且 MUST 由无凭据的规范化输入生成；
- mapping 冲突 MUST fail closed；
- loser EarningsEvent MUST 保持 candidate，不删除、不覆盖、不 copy 历史；
- loser MUST NOT 进入未来公开 canonical selector。

### 6.5 `MonitoringPoolSnapshot` / `MonitoringPoolMember`（4.2D-1 已实现）

> ADR-012 已冻结 selector contract；4.2D-1 已实现 selector core、append-only snapshot 与
> member persistence。Scheduled command integration 仍待后续阶段。

Stage 4.2 monitoring pool 的 canonical unit 是 Company。Snapshot 是 historical run fact；
不得用 current index policy 或 later correction 回写。

| `MonitoringPoolSnapshot` 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `as_of_date` | `America/New_York` 业务自然日 |
| `selector_version` | 例如 `earnings-monitoring-pool-v1` |
| `enabled_index_codes` | canonical JSON array |
| `input_revision` | canonical selector input manifest SHA-256 |
| `pool_hash` | canonical output/input envelope SHA-256 |
| `member_count` | 非负整数 |
| `created_at` | UTC |

| `MonitoringPoolMember` 字段 | 说明 |
|---|---|
| `snapshot_id` | FK snapshot，PROTECT |
| `company_id` | FK Company，PROTECT |
| `ordinal` | Company UUID canonical order |
| `basis` | `index_code + security_listing_id + effective_from + effective_to` canonical JSON |

约束：

- snapshot `(as_of_date, selector_version, pool_hash)` 与
  `(as_of_date, selector_version, input_revision)` unique；
- member `(snapshot, company)` 与 `(snapshot, ordinal)` unique；
- snapshot/member append-only；hash 或 input revision reload mismatch 时 fail closed；
- Provider query identity 不属于本 schema；其 provider-specific projection 在 4.2F 定义。

Selector 使用 `effective_from <= as_of < effective_to`，按 Company 去重。同一输入必须得到
相同 member order、input revision 和 pool hash。Late-arriving correction 可以形成新的
snapshot revision，但不能覆盖既有 run fact。

### 6.6 Earnings Candidate / Company Matching（4.2D-2 planned）

> ADR-013 已冻结 4.2D-2 matching contract；以下行为尚未实现，不代表 current fact。

Candidate 复用现有 `EarningsEvent` row，不新增 `EarningsCandidate` 表：

```text
EarningsEvent(identity_status=candidate)
```

Matching 只在 frozen `MonitoringPoolSnapshot.members` 的 Company 范围内进行，匹配日期固定为
`monitoring_pool_as_of`。V1 只允许 exact CIK 或 exact ticker + canonical exchange。
`provider_symbol` 与 company name 只作 evidence，不参与自动匹配，不使用 fuzzy matching。
Snapshot member basis 授权 Company；listing evidence 可以使用该 Company 在 as-of 有效的
SecurityListing，但不会扩大 Company pool。

匹配结果使用现有 append-only `EarningsReconciliationDecision`：

- `MATCHED` -> `created_candidate` / `resolved` / target candidate；
- `UNMATCHED` -> `no_match` / `rejected` / no candidate；
- `AMBIGUOUS` -> `review_required` / `open` / no candidate；
- `OUT_OF_POOL` -> `ignored` / `rejected` / no candidate。

`match_factors.company_match` 保存 matcher version、matching input revision、snapshot
identity、规范化的 matching hints、匹配策略、matching date 和 SecurityListing evidence。
`match_execution_key` 用于所有 matching outcome；`match_result_key` 用于 append-only
decision identity；只有 `MATCHED` 才由 observation、snapshot semantic identity、matcher
version、matching input revision、matched Company 和 listing evidence 派生 deterministic
candidate identity。非匹配结果没有 Candidate UUID。

该规划不新增 schema、不创建 migration、不改变 MonitoringPoolSnapshot 或 EarningsEvent 字段。

## 7. SEC 文件

### 7.1 `Filing`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `accession_number` | unique，规范化 SEC accession number |
| `form_type` | 8-K / 10-Q / 10-K / 6-K / 20-F / 40-F / other |
| `accepted_at` | timestamptz |
| `period_of_report` | date nullable |
| `primary_document` | string |
| `filing_url` | URL |
| `exhibit_url` | URL nullable；多个附件时拆表 |
| `is_earnings_related` | boolean/tri-state |
| `classification_rule_version` | string nullable |
| `source_evidence_id` | SEC 来源证据 |
| `created_at`, `updated_at` | UTC |

若一个 filing 有多个相关附件，使用 `FilingDocument(id, filing_id, document_type, sequence, filename, url, description)`，而不是只保留一个 exhibit URL。

### 7.2 `FilingEarningsLink`

| 字段 | 说明 |
|---|---|
| `filing_id`, `earnings_event_id` | FK |
| `relation_type` | RELEASE_FILING / PERIODIC_FILING / OTHER；具体表单类型来自 Filing |
| `release_filing_classification` | YES / NO / REVIEW_REQUIRED nullable；只对 release 候选使用 |
| `classification_reason` | 分类原因或命中证据摘要 |
| `classification_rule_version` | 分类规则版本 |
| `confidence` | 自动匹配置信度 |
| `match_rule_version` | 规则版本 |
| `review_status` | auto / confirmed / rejected |
| `created_at`, `reviewed_at`, `reviewed_by` | 审核信息 |

唯一约束：`filing + earnings_event + relation_type`。

### 7.3 财报页面的 Filing 派生状态

不在 EarningsEvent 上保存单一 `filing_status`，以免再次压缩两个独立维度。查询层从未被 rejected 的 FilingEarningsLink 推导：

- `has_release_filing`：存在 `RELEASE_FILING` 关联且 `release_filing_classification=YES`；美国公司通常为含财报材料的 8-K，外国发行人可为 6-K；
- `has_periodic_filing`：存在 `PERIODIC_FILING` 关联；通常为 10-Q、10-K、20-F 或 40-F；
- 每一类同时返回关联 Filing 的 form type、accepted_at 和 URL，而不仅是布尔值。

REVIEW_REQUIRED 不计为已提交，页面可按产品策略显示“待复核”。8-K/6-K 表单类型本身不能直接得出 YES。两个派生值彼此独立、与 EarningsEvent.status 也独立。允许 `RELEASED + has_release_filing=true + has_periodic_filing=false`，页面显示“财报已发布、8-K 已提交、10-Q 待提交”。文件先后顺序不触发财报生命周期倒退或前进。若未来为性能缓存派生值，缓存不能成为事实来源，并必须有一致性重算测试。详见 ADR-003。

## 8. 自选股与提醒规则

### 8.1 `WatchlistItem`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `user_id`, `company_id` | FK |
| `priority_level` | normal / important |
| `alerts_enabled` | 单家公司总开关 |
| `is_active` | 是否有效；移除时保留历史 |
| `created_at`, `updated_at`, `deactivated_at` | UTC |

唯一约束：`user + company` 一条主记录；重新添加时激活并保留审计。有效记录变化触发 Company 监控状态重算。

### 8.2 `ReminderRule`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `user_id` | FK User |
| `company_id` | FK Company nullable；空表示用户默认规则 |
| `event_type` | earnings_upcoming / date_changed / earnings_released / filing / index_added / index_removed / index_transferred / full_exit |
| `channel` | email / in_app |
| `lead_time_minutes` | 仅 upcoming 使用；MVP UI 为 1 天 |
| `is_enabled` | boolean |
| `created_at`, `updated_at` | UTC |

唯一约束：`user + company(null-safe) + event_type + channel + lead_time`。公司规则覆盖还是叠加用户默认规则必须明确；建议采用“更具体规则覆盖默认规则”。

## 9. 通知记录

### 9.1 `Notification`

代表一个用户、一个渠道的一条业务通知，也是 MVP 的持久待发送队列。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `user_id` | FK User |
| `reminder_rule_id` | FK nullable |
| `event_type` | 稳定枚举 |
| `related_object_type`, `related_object_id` | 受限 polymorphic 引用；服务校验允许类型 |
| `event_version` | 触发数据版本/变更 ID |
| `priority` | P0 / P1 / P2 / P3 |
| `channel` | email / in_app |
| `status` | pending / processing / sent / failed / cancelled / suppressed |
| `idempotency_key` | unique |
| `scheduled_at`, `claimed_at`, `sent_at` | timestamptz nullable |
| `attempt_count`, `next_attempt_at` | 重试状态 |
| `subject_snapshot`, `body_snapshot` | 发送时内容；隐私保留期限待确认 |
| `source_url` | 用户可见来源 nullable |
| `last_error_code`, `last_error_message` | 脱敏错误 |
| `read_at` | 站内通知阅读时间 nullable |
| `created_at`, `updated_at` | UTC |

推荐幂等键输入：`user + rule/effective-policy + event_type + related_object + event_version + channel + digest_bucket`。不要把发送尝试次数放入键中。

### 9.2 `NotificationDeliveryAttempt`

| 字段 | 说明 |
|---|---|
| `notification_id` | FK |
| `attempt_number` | 从 1 递增 |
| `provider_message_id` | 邮件服务 ID nullable |
| `started_at`, `finished_at` | UTC |
| `outcome` | accepted / temporary_failure / permanent_failure / unknown |
| `error_code`, `error_message` | 脱敏信息 |
| `response_metadata` | 受控 JSON，不存凭据 |

唯一约束：`notification + attempt_number`。通知最终状态不覆盖尝试历史。

## 10. 数据来源、原始数据与同步运行

### 10.1 `DataSource`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `key`, `name` | 稳定唯一键/展示名 |
| `source_type` | sec / ir / earnings_calendar / index / manual |
| `base_url` | 来源入口；模型/Admin 校验禁止 URL userinfo、真实敏感查询值和明显认证凭据 |
| `is_official` | boolean |
| `provider_adapter` | 适配器标识，不存密钥 |
| `license_notes` | 许可摘要/链接 |
| `is_enabled` | boolean |
| `created_at`, `updated_at` | UTC |

### 10.2 `SyncRun`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK，作为 task/run id |
| `job_type`, `source_id` | 任务和来源 |
| `scope` | 受控 JSON，如指数/CIK/日期范围 |
| `idempotency_key` | 同一计划窗口唯一 nullable |
| `status` | running / succeeded / partial / failed / skipped |
| `run_mode` | ingestion / replay；默认 ingestion |
| `replay_source_sync_run_id` | 可选 PROTECT self-FK；replay 必须指向 terminal ingestion run |
| `replay_contract_version` | replay 语义版本；非 replay 行为空 |
| `replay_input_digest` | replay 消费完整 raw manifest 的 SHA-256；非 replay 行为空 |
| `started_at`, `finished_at`, `heartbeat_at` | UTC |
| `fetched_count`, `replayed_count`, `created_count`, `updated_count`, `skipped_count`, `failed_count` | PRD 要求统计；replay 的 `fetched_count` 必须为 0，`replayed_count` 从 replay-linked RawDataObservation 重建 |
| `error_summary` | 脱敏摘要 |
| `code_version`, `parser_version` | 可重现性 |
| `provider_version` | nullable provider contract/version provenance；历史 run 可保持 NULL，新 earnings-calendar ingestion 必须持久化，replay 要求非空 |

4.2C-6 replay foundation 的约束：

- `run_mode=ingestion` 的行不得携带 replay source、replay contract metadata 或 replay progress；
- `run_mode=replay` 的行必须具有 `window_kind=replay`、source lineage、非空 parser/contract
  version、非空 provider version、64 位小写 SHA-256 input digest，且 `fetched_count=0`；
- replay source FK 禁止自引用并使用 `PROTECT`；服务验证 source 与 replay 的 DataSource、
  job type 相同，replay scope 与 source scope 除 `window_kind` 外完全一致，source 为
  terminal ingestion run，raw observation count 与 source `fetched_count` 一致；
- partial unique constraint 固化 replay identity：source、job type、source SyncRun、parser
  version、contract version 与 input digest 相同不得创建第二条 replay run；
- historical SyncRun 迁移时全部标记为 ingestion，新增 metadata 为空、`replayed_count=0`，
  不回填或猜测历史 replay lineage。
- `provider_version` 是 run-level immutable parser context：同一 run 的所有 page 必须一致；
  历史 NULL 表示 provenance unknown 且 replay-ineligible，不得从 normalized observation 或
  current provider version 推断。
- Direct ORM creation of earnings-calendar SyncRuns bypasses these service invariants and is
  unsupported for production writes. `audit/0009` downgrade removes replay-only semantics and
  is not a lossless operation for data containing replay runs.

### 10.3 `RawDataRecord`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `source_id` | FK DataSource |
| `first_sync_run_id` | 首次获取任务 |
| `source_url` | 具体 URL；userinfo 被拒绝，敏感查询值替换为稳定标记，fragment 不保存 |
| `request_fingerprint` | 去除密钥后的稳定请求指纹；保留并排序安全查询条件，不包含 fragment |
| `fetched_at` | UTC |
| `http_status`, `content_type`, `encoding` | 响应元数据 |
| `content_hash` | 原始字节哈希 |
| `payload` | 原始 bytes；MVP 受控存 DB |
| `payload_size_bytes` | 原始 bytes 长度；必须与 payload 实际长度一致 |
| `parser_status`, `parser_version`, `parse_error` | 解析状态 |
| `created_at` | UTC |

唯一约束：`source + request_fingerprint + content_hash`。请求指纹不包含凭据值；同一业务请求只更换 API key 时指纹不变，安全参数变化时仍能区分请求。内容哈希按原始 bytes 计算。初始数据库硬上限为 1 MiB，运行配置只能下调；这是工程保护值，不替代仍待确认的长期保留和容量政策。明显包含认证字段或 Authorization/Basic/Bearer 凭据的正文在写库前拒绝，错误只返回不含秘密的通用说明。

### 10.4 `RawDataObservation`

用于证明一次同步运行观察到了已存在的原始正文，而不复制 payload。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `sync_run_id` | 本次观察所属 SyncRun |
| `raw_data_record_id` | 被观察的去重正文 |
| `observed_at` | 时区感知 UTC 时刻 |

唯一约束：`sync_run + raw_data_record`。同一次运行幂等重跑不新增观察；后续运行再次看到相同正文时复用 RawDataRecord，并追加自己的观察记录。

### 10.5 `SourceEvidence`

将标准化值和领域记录连到原始数据。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `raw_data_record_id`, `sync_run_id` | 来源链；source 从 RawDataRecord 唯一追溯，不冗余存储 |
| `target_type`, `target_id` | 受限枚举 + 稳定 UUID；不使用 GenericForeignKey |
| `field_name` | 可空字符串；空代表整条记录 |
| `raw_value`, `normalized_value` | JSON；共享安全模块递归检查 dict/list/tuple，service 拒绝凭据键及显式 Authorization/Basic/Bearer 文本 |
| `is_official` | 创建时从 RawDataRecord.source 快照派生 |
| `confidence` | Decimal，范围 `0.0000–1.0000` |
| `observed_at` | 从对应 RawDataObservation 取得的时区感知 UTC 时刻 |
| `normalizer_version` | 非空规则版本 |
| `evidence_key` | SHA-256 稳定唯一幂等键 |
| `created_at` | UTC |

`target_type` 首版只允许 Company、SecurityListing、MarketIndex、IndexMembership、IndexChangeEvent、IndexChangeLeg、EarningsEvent、EarningsDateChange、Filing、FilingDocument 和 FilingEarningsLink。audit app 不导入这些未来业务 app；后续领域 service 负责确认 target UUID 对应对象存在。

4.1B 的 EarningsDateChange 不写入 target=`EarningsDateChange` 的 SourceEvidence。日期变化的来源证据 target 保持 `EarningsEvent`，并由 DataChange 的直接 FK 关联；`EarningsDateChange` 枚举值仅为兼容保留，当前 contract 不使用，启用独立 target 前必须新增 ADR。

数据库使用复合外键要求 `SourceEvidence(sync_run_id, raw_data_record_id)` 对应已存在的 `RawDataObservation(sync_run_id, raw_data_record_id)`；写入 service 还校验 SyncRun 与 RawDataRecord 的 DataSource 一致。幂等键输入为 `raw_data_record_id + target_type + target_id + field_name + canonical normalized_value + normalizer_version`，不包含 raw body、DataSource、SyncRun、confidence 或 observed_at。同一 RawDataRecord 对同一目标字段产生相同标准化值和规则版本时复用原证据；不同 RawDataRecord 即使来自同一 DataSource 且标准化值相同，也分别保存证据，以保持每份原始文档的独立追溯链。

同一领域记录仍可因不同来源、字段、标准化值或规则版本拥有多条证据；领域表中的 `primary_source_evidence_id` 只是当前选中来源的快捷引用。领域 Service 使用证据时只接受其主键作为入口，重新加载持久化的 SourceEvidence、RawDataRecord、DataSource、SyncRun 和 RawDataObservation；证据 target 必须匹配领域目标，显式传入的 SyncRun 必须与证据的持久化 SyncRun 相同，并且该任务必须观察过该原始记录。调用方在内存中修改 evidence 的 target、来源或任务字段不能改变验证结果。

ADR-010 进一步冻结：尚未匹配到 EarningsEvent 的 provider observation MUST NOT 创建
SourceEvidence，其 provenance 保留在 `EarningsCalendarObservation` 与 raw / observation 链中。
4.2A 不扩展 SourceEvidence target enum。4.2B 已按 ADR-010 扩展 AuditRecord target enum，
新增 `earnings_reconciliation_decision`；`EarningsCalendarObservation` 本身不新增 AuditRecord
target。SourceEvidence 仍只指向 EarningsEvent 等既有领域目标。

`0004_rekey_source_evidence_by_raw_record` 只正向重算 evidence_key，不删除、合并或改写证据内容；其反向迁移为 noop。若需要回退代码，必须先评估旧版 key 语义与当前数据的兼容性，不能假定反向迁移会恢复旧 key。

## 11. 变更历史与审计记录

### 11.1 `DataChange`

记录自动同步或管理员修正导致的关键字段变化。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `target_type`, `target_id` | 受限领域枚举 + 稳定 UUID；不使用 GenericForeignKey |
| `field_name` | 非空变化字段；不得是密码、Token 等敏感字段名 |
| `old_value`, `new_value` | 规范化 JSON；必须不同并通过集中凭据检查 |
| `source_evidence_id` | 导致变化的来源证据 nullable，`PROTECT` |
| `sync_run_id` | 自动任务 nullable，`PROTECT` |
| `actor_user_id` | 手工操作人 nullable，`PROTECT` |
| `reason` | 人工修正必填；自动变化可空 |
| `origin_key` | 稳定变化来源；人工使用调用方请求/操作键，自动从 SourceEvidence 或 SyncRun 派生 |
| `rule_version` | 非空变化检测/修正规则版本 |
| `change_key` | 64 位小写 SHA-256，unique |
| `changed_at`, `created_at` | 时区感知 UTC 时刻 |

`target_type` 与 SourceEvidence 使用相同的领域集合：Company、SecurityListing、MarketIndex、IndexMembership、IndexChangeEvent、IndexChangeLeg、EarningsEvent、EarningsDateChange、Filing、FilingDocument 和 FilingEarningsLink。audit app 只保存类型和值，不导入未来业务 app；领域 service 负责确认目标 UUID 存在。

`change_key` 的规范化输入为：

```text
target_type
+ target_id
+ field_name
+ canonical_old_value
+ canonical_new_value
+ source_identity
+ rule_version
```

`source_identity` 对人工修正使用 `actor_user_id + origin_key`，对自动变化优先使用 `source_evidence_id`，没有证据时使用 `sync_run_id`。因此同一来源和规则的相同变化重跑时复用原记录；不同证据，或无证据时的不同任务，可以分别追溯。人工修正必须提供 actor、reason 和稳定 origin_key；自动变化必须至少提供 SourceEvidence 或 SyncRun。SourceEvidence 与 SyncRun 同时提供时，service 验证该任务确实观察过证据对应的 RawDataRecord，且 DataSource 一致。

数据库限制 target_type、非空字段/版本/来源键、change_key 格式、人工/自动来源组合，并用 PostgreSQL JSON 相等性拒绝 old_value 与 new_value 相同。Service 先做规范化比较，值相同直接返回 skipped，不创建记录。

专门业务表 `EarningsDateChange` 用于产品语义和通知；`DataChange` 用于统一字段级追踪。4.1B 使用以下真实关系：

```text
EarningsDateChange.data_change_id
    -> DataChange.id
    -> DataChange.source_evidence_id
    -> SourceEvidence.id
```

DataChange 的 `target_type=earnings_event`、`target_id=EarningsEvent.id`，`field_name` 为受控字段名。EarningsDateChange 不复制 DataChange 的 `change_key` 或 source evidence；领域 Service 必须在同一个事务中创建并校验两者。

### 11.2 `AuditRecord`

记录安全和管理行为，不与数据来源证据混用。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `actor_user_id` | 人工操作者 nullable，`PROTECT` |
| `sync_run_id` | 自动任务或人工触发任务 nullable，`PROTECT` |
| `action` | create / update / deactivate / manual_correction / manual_sync / retry / login_sensitive_action |
| `target_type`, `target_id` | 受限枚举 + 稳定 UUID；不使用 GenericForeignKey |
| `before`, `after` | 受控 JSON；递归拒绝密码、Session、Cookie、认证头、Token、API key 和 URL 凭据 |
| `reason` | 任何带 actor_user 的人工操作必填 |
| `request_id` | 非空请求/任务操作 ID；不要求全表唯一 |
| `ip_hash` | 可空；仅保存 `v1:` + 64 位小写十六进制 keyed HMAC-SHA256，不保存原始 IP |
| `audit_key` | 64 位小写 SHA-256，unique，用于相同操作重试幂等复用 |
| `created_at` | UTC |

AuditRecord 的 target_type 首版允许 User、DataSource、SyncRun、RawDataRecord、RawDataObservation、SourceEvidence、DataChange 以及上述领域目标。人工操作必须有 actor_user 和 reason；系统操作必须有 SyncRun；两者可以同时存在，但至少要有一个明确来源。`audit_key` 由 actor/sync、action、target、canonical before/after、reason 和 request_id 生成。ip_hash 不参与幂等键，避免哈希密钥轮换改变同一操作的身份；重试复用首次记录的 ip_hash。相同输入连续执行两次复用原记录，不提供可更新审计内容的 Service。

IP 哈希 v1 使用独立环境变量 `AUDIT_IP_HASH_KEY` 和公开 context `earnings-radar.audit.ip-hash.v1`；Django `SECRET_KEY` 不参与计算，也不能作为生产回退值。生产及其他非 development/test 环境缺少独立密钥、使用开发默认值或与 `DJANGO_SECRET_KEY` 相同时拒绝启动。`v1` 前缀标识算法/context 版本，旧记录保持原值；轮换当前密钥后只影响后续新操作，不批量重算历史。若未来需要明确区分轮换代次，则新增 v2 前缀/context 并保留 v1，而不是覆盖旧值。

DataChange 和 AuditRecord 都是追加式历史：模型实例拒绝更新和删除，Admin 只提供受权限控制的查看、筛选和截断 JSON 预览。QuerySet update/delete 与直接 SQL 仍可绕过模型方法，因此长期规范禁止业务代码使用这些路径；只有经过评审的数据库迁移可处理历史数据。保留期限和具体查看角色仍待确认。

## 12. 关键唯一约束与幂等键

| 对象 | 约束/幂等依据 |
|---|---|
| Company | 非空规范化 CIK unique |
| SecurityListing | exchange + ticker + 不重叠有效期 |
| MarketIndex | code unique |
| IndexMembership | security_listing + index + 不重叠有效期 |
| IndexChangeEvent | aggregation_key unique |
| EarningsEvent | 非空 identity_key unique；规则为 company + period_end_date + period_type，带版本 |
| EarningsDateChange | data_change unique；领域历史 append-only |
| EarningsCalendarObservation（4.2B 已实现） | raw_data_record + parser_version + provider_event_id unique；按 source + provider_event_id 建索引 |
| EarningsReconciliationDecision（4.2B 已实现） | deterministic decision_key unique；append-only；supersedes 链 |
| Filing | accession_number unique |
| WatchlistItem | user + company unique |
| ReminderRule | null-safe user/company/event/channel/lead unique |
| Notification | idempotency_key unique |
| RawDataRecord | source + request_fingerprint + content_hash unique |
| SourceEvidence | evidence_key unique；raw data record + target + field + normalized value + normalizer version |
| DataChange | change_key unique |
| AuditRecord | audit_key unique；actor/sync + action + target + before/after + reason + request |
| MonitoringPoolSnapshot（4.2D-1 已实现） | as_of + selector_version + pool_hash / input_revision unique；append-only |
| MonitoringPoolMember（4.2D-1 已实现） | snapshot + company unique；snapshot + ordinal unique |
| SyncRun replay identity | source + job_type + replay_source_sync_run + parser_version + replay_contract_version + replay_input_digest unique（仅 run_mode=replay） |

并发写入必须捕获唯一冲突后读取已存在记录，不能依赖“先查后写”。

## 13. 删除、保留和隐私

- 公司退出监控池：停止未来同步，保留 Company 及全部历史；
- 自选股移除：停用 WatchlistItem，保留与通知审计相关的历史；
- 来源停用：不级联删除 RawDataRecord 或 SourceEvidence；
- 用户删除请求与开源自托管场景下的数据保留/匿名化规则，PRD 未定义，需要产品与法律确认；
- 原始响应、通知正文、审计 IP 信息的期限必须在上线前明确；
- API 密钥、密码、session、邮件认证信息绝不进入原始数据或审计 JSON。

## 14. 数据决策状态

Stage 4.2A 已由 ADR-010 冻结、不再属于待确认的决策：

- provider external identity 分层、missing ID 处理与 provider-level 要求；
- exact-only cross-provider automatic matching，不实现 fuzzy threshold；
- 4.2 third-party calendar 字段权限与 append-only manual decision authority；
- sync window / backfill / empty calendar 语义；
- no destructive merge、loser 保留与 canonical collision 处理；
- monitoring pool、scope、pagination 与 replay identity。

ADR-012 进一步冻结 4.2D selector：canonical unit 为 Company；Stage 4.2 universe 仅来自
explicit enabled index policy 的 as-of normative IndexMembership；历史结果采用 Hybrid
snapshot；retry/replay 不重选。4.2D-1 已实现 snapshot schema、selector core、canonical
input revision、pool hash 与并发幂等；scheduled command integration 尚未实现。

以下数据决策仍待确认：

1. precision refinement / regression 是否通知用户，以及日期变化通知中的 old/new status 组成；历史记录规则已由 ADR-007 确定。
2. 公司无 CIK、CIK 变更、ticker 重用、ADR/多上市身份的合并规则；4.2 matching 已由 ADR-010 限定为 unique CIK 或 unique exchange+ticker as-of，其余 fail closed，但 Company 主数据合并规则仍需确认。
3. `/companies/{ticker}` 遇到历史 ticker 或跨交易所歧义时的行为。
4. 1–7 日指数偏移候选的人工复核负责人、处理时限与默认行为。
5. release filing 首版 exhibit/文本证据清单、REVIEW_REQUIRED 展示范围和复核时限。
6. IR / SEC 等高 authority 来源的字段级冲突矩阵与复核流程（4.4 / 4.5 前）；4.2 第三方 calendar 的字段权限与 append-only manual decision authority 已由 ADR-010 确定。
7. 用户级与公司级 ReminderRule 的覆盖/叠加规则。
8. 提醒“提前一天”按美东日期还是用户本地日期，以及夏令时边界。
9. 原始数据、通知内容、审计记录和已停用用户数据的保留期限。
10. AuditRecord 和 DataChange 的保留期限、IP 哈希保留期及具体查看角色仍需在阶段 8.1 前确认；目标引用已确定为受限枚举 + UUID，不使用 Django ContentType 或 GenericForeignKey。
11. 4.2F 最终 provider / license checklist 结论与 anomaly shrink operational 阈值；不阻塞 4.2C-4.2E。
