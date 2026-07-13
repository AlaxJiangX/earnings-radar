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
  +---* ReminderRule                  +---* EarningsEvent 1---* EarningsDateChange
  +---* Notification                  |          |
                                      |          *---* Filing (via FilingEarningsLink)
  |
  +---* IndexChangeLeg *---1 IndexChangeEvent

DataSource 1---* RawDataRecord *---1 SyncRun
                     |
                     +---* SourceEvidence ---* domain records

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

约束：同一交易所和 ticker 的有效期不能重叠；同一公司同一时点可以有多个 listing/share class，但最多一个默认展示代码（可用条件约束/服务校验）。搜索索引覆盖 ticker 和公司名称。URL 使用 ticker 时应处理历史代码与歧义；长期建议内部 canonical URL 使用稳定公司 ID，但是否改变 PRD 路由待确认。

## 5. 指数及成分关系

### 5.1 `MarketIndex`

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

保存当前和历史指数成分关系。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `index_id`, `security_listing_id` | FK；成员身份落到具体证券 |
| `effective_from` | date |
| `effective_to` | date nullable；半开区间结束 |
| `announcement_date` | date nullable |
| `status` | announced / active / ended / cancelled / corrected |
| `last_verified_at` | timestamptz |
| `source_evidence_id` | 当前关系的主要证据 |
| `created_at`, `updated_at` | UTC |

约束：同一 SecurityListing 与指数的生效区间不得重叠，membership 的有效期必须与 listing 有效期相交且不能超出已知 listing 生命周期。不能只依赖冗余 `is_active`，当前有效性由状态与日期推导。若为查询性能保留 `is_active`，必须由服务统一维护并有一致性测试。

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
| `estimated_release_at` | timestamptz nullable |
| `confirmed_release_at` | timestamptz nullable |
| `earnings_release_at` | timestamptz nullable |
| `conference_call_at` | timestamptz nullable |
| `release_session` | pre_market / after_market / during_market / unknown |
| `status` | SCHEDULED_ESTIMATED / SCHEDULED_CONFIRMED / RELEASED / CANCELLED |
| `confidence` | 可解释等级或数值，算法待确认 |
| `primary_source_evidence_id` | 当前主证据 |
| `created_at`, `updated_at` | UTC |

正式唯一身份已确定为 `company_id + period_end_date + period_type`，并由带版本的规范化函数生成 `identity_key`。年度财报统一为 `FY + includes_q4=true`，上游 Q4 年度标签不另建事件。52/53 周通过 `fiscal_calendar_type` 和 `period_length_weeks` 表达，不作为 period_type。`fiscal_year` 是来源/展示属性，不参与唯一键。数据库对非空 `identity_key` 设置唯一约束，并要求 CANONICAL 事件必须有 `period_end_date`、`period_type`、`identity_key` 和 `identity_rule_version`。

当 `period_end_date` 未知时，只能创建 CANDIDATE 事件：它依赖 Provider 的外部事件标识和来源证据去重，不能使用 `company + fiscal_year + period_type` 作为正式身份。日期确定后由核对服务匹配或提升为 CANONICAL；候选合并、拆分和提升必须保留旧标识、来源及 DataChange，不能静默覆盖。详细决策见 ADR-001。

EarningsEvent.status 只回答“财报安排/发布到了哪一步”，不回答 SEC 文件是否提交。取消后重新安排是恢复原事件还是新候选事件仍待确认。

### 6.2 `EarningsDateChange`

虽然名称为日期变化，记录中同时保存状态上下文。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `earnings_event_id` | FK |
| `field_name` | estimated_release_at / confirmed_release_at / conference_call_at 等允许字段 |
| `old_value`, `new_value` | timestamptz nullable |
| `old_status`, `new_status` | enum nullable |
| `is_official` | boolean |
| `change_key` | unique stable idempotency key |
| `source_evidence_id` | 导致变化的证据 |
| `detected_at` | timestamptz |

只有规范化后值变化才创建记录。数据精度（仅日期、盘前/盘后、具体时刻）应另存 precision，避免把“日期相同但精度提升”误判为日期变化；字段设计在实现前确认。

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
| `base_url` | 来源入口 |
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
| `started_at`, `finished_at`, `heartbeat_at` | UTC |
| `fetched_count`, `created_count`, `updated_count`, `skipped_count`, `failed_count` | PRD 要求统计 |
| `error_summary` | 脱敏摘要 |
| `code_version`, `parser_version` | 可重现性 |

### 10.3 `RawDataRecord`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `source_id` | FK DataSource |
| `first_sync_run_id` | 首次获取任务 |
| `source_url` | 具体 URL |
| `request_fingerprint` | 去除密钥后的稳定请求指纹 |
| `fetched_at` | UTC |
| `http_status`, `content_type`, `encoding` | 响应元数据 |
| `content_hash` | 原始字节哈希 |
| `payload` | JSON/text/binary reference；MVP 受控存 DB |
| `parser_status`, `parser_version`, `parse_error` | 解析状态 |
| `created_at` | UTC |

唯一建议：`source + request_fingerprint + content_hash`。为证明后续运行看到了同一响应，可选 `RawDataObservation(sync_run_id, raw_data_record_id, observed_at)`，唯一为 `run + raw record`，不会复制正文。

### 10.4 `SourceEvidence`

将标准化值和领域记录连到原始数据。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `raw_data_record_id`, `sync_run_id`, `source_id` | 来源链 |
| `entity_type`, `entity_id` | 受限领域对象引用 |
| `field_name` | 可空；空代表整条记录 |
| `raw_value`, `normalized_value` | JSON，敏感字段需过滤 |
| `is_official`, `confidence` | 来源性质和可信度 |
| `observed_at` | UTC |
| `normalizer_version` | 规则版本 |

同一领域记录可有多条证据；领域表中的 `primary_source_evidence_id` 只是当前选中来源的快捷引用。

## 11. 变更历史与审计记录

### 11.1 `DataChange`

记录自动同步或管理员修正导致的关键字段变化。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `entity_type`, `entity_id` | 受限领域对象 |
| `field_name` | 变化字段 |
| `old_value`, `new_value` | JSON |
| `source_evidence_id` | 来源证据 nullable |
| `sync_run_id` | 自动任务 nullable |
| `actor_user_id` | 手工操作人 nullable |
| `reason` | 管理员修正必填 |
| `change_key` | unique 幂等键 |
| `changed_at` | UTC |

专门业务表（如 `EarningsDateChange`）用于产品语义和通知；`DataChange` 用于统一字段级追踪，两者可通过相同 change key/引用关联。

### 11.2 `AuditRecord`

记录安全和管理行为，不与数据来源证据混用。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `actor_user_id` | 用户 nullable；系统任务为空 |
| `sync_run_id` | 自动任务 nullable |
| `action` | create / update / deactivate / manual_sync / retry / login_sensitive_action 等 |
| `object_type`, `object_id` | 目标对象 |
| `before`, `after` | 受控 JSON；不保存密码/令牌 |
| `reason` | 管理修正必填 |
| `request_id`, `ip_hash` | 可选追踪信息；保留政策待确认 |
| `created_at` | UTC |

审计记录追加写入、普通管理员不可编辑。保留期限和谁能查看待确认。

## 12. 关键唯一约束与幂等键

| 对象 | 约束/幂等依据 |
|---|---|
| Company | 非空规范化 CIK unique |
| SecurityListing | exchange + ticker + 不重叠有效期 |
| MarketIndex | code unique |
| IndexMembership | security_listing + index + 不重叠有效期 |
| IndexChangeEvent | aggregation_key unique |
| EarningsEvent | 非空 identity_key unique；规则为 company + period_end_date + period_type，带版本 |
| EarningsDateChange | change_key unique |
| Filing | accession_number unique |
| WatchlistItem | user + company unique |
| ReminderRule | null-safe user/company/event/channel/lead unique |
| Notification | idempotency_key unique |
| RawDataRecord | source + request_fingerprint + content_hash unique |
| DataChange | change_key unique |

并发写入必须捕获唯一冲突后读取已存在记录，不能依赖“先查后写”。

## 13. 删除、保留和隐私

- 公司退出监控池：停止未来同步，保留 Company 及全部历史；
- 自选股移除：停用 WatchlistItem，保留与通知审计相关的历史；
- 来源停用：不级联删除 RawDataRecord 或 SourceEvidence；
- 用户删除请求与开源自托管场景下的数据保留/匿名化规则，PRD 未定义，需要产品与法律确认；
- 原始响应、通知正文、审计 IP 信息的期限必须在上线前明确；
- API 密钥、密码、session、邮件认证信息绝不进入原始数据或审计 JSON。

## 14. 待确认的数据决策

1. 候选财报事件跨多个 Provider 的自动合并阈值，以及取消后重新安排的身份处理。
2. 只有“日期 + 盘前/盘后”时如何表达精度，以及从日期升级为具体时刻是否通知。
3. 公司无 CIK、CIK 变更、ticker 重用、ADR/多上市身份的合并规则。
4. `/companies/{ticker}` 遇到历史 ticker 或跨交易所歧义时的行为。
5. 1–7 日指数偏移候选的人工复核负责人、处理时限与默认行为。
6. release filing 首版 exhibit/文本证据清单、REVIEW_REQUIRED 展示范围和复核时限。
7. 来源可信度的量表、冲突胜出矩阵及人工修正是否锁定字段。
8. 用户级与公司级 ReminderRule 的覆盖/叠加规则。
9. 提醒“提前一天”按美东日期还是用户本地日期，以及夏令时边界。
10. 原始数据、通知内容、审计记录和已停用用户数据的保留期限。
11. polymorphic 来源/审计引用采用 Django ContentType 还是显式引用表；实现前需以查询和完整性测试做 ADR。
