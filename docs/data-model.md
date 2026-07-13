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
User 1---* WatchlistItem *---1 Company 1---* SecurityListing
  |              |                    |             |
  |              *---* ReminderRule   |             +-- ticker history
  |                                   |
  +---* ReminderRule                  +---* EarningsEvent 1---* EarningsDateChange
  +---* Notification                  |          |
                                      |          *---* Filing (via FilingEarningsLink)
                                      |
Index 1---* IndexMembership *---------+
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
| `is_primary` | 当前是否为主要展示代码 |
| `effective_from`, `effective_to` | date；有效期半开区间 |
| `source_evidence_id` | 当前关系的主要来源证据 |
| `created_at`, `updated_at` | UTC |

约束：同一交易所和 ticker 的有效期不能重叠；同一公司同一时点最多一个主代码（可用条件约束/服务校验）。搜索索引覆盖 ticker 和公司名称。URL 使用 ticker 时应处理历史代码与歧义；长期建议内部 canonical URL 使用稳定公司 ID，但是否改变 PRD 路由待确认。

## 5. 指数及成分关系

### 5.1 `MarketIndex`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `code` | unique，如 `SP500`, `NASDAQ100`, `DJIA`, `RUSSELL2000` |
| `name` | 展示名称 |
| `provider_symbol` | Provider 使用的代码 nullable |
| `tier` | 排序层级；具体顺序待确认 |
| `is_enabled` | 是否纳入监控池 |
| `created_at`, `updated_at` | UTC |

### 5.2 `IndexMembership`

保存当前和历史指数成分关系。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `index_id`, `company_id` | FK |
| `effective_from` | date |
| `effective_to` | date nullable；半开区间结束 |
| `announcement_date` | date nullable |
| `status` | announced / active / ended / cancelled / corrected |
| `last_verified_at` | timestamptz |
| `source_evidence_id` | 当前关系的主要证据 |
| `created_at`, `updated_at` | UTC |

约束：同一公司与指数的生效区间不得重叠；不能只依赖冗余 `is_active`，当前有效性由状态与日期推导。若为查询性能保留 `is_active`，必须由服务统一维护并有一致性测试。

### 5.3 `IndexChangeEvent`

面向业务展示和通知的变化聚合。

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `change_type` | ADDED / REMOVED / TRANSFERRED / PARTIAL_EXIT / FULL_EXIT / MULTI_INDEX_ADDITION / REENTERED / UPDATED / CANCELLED |
| `announcement_date`, `effective_date` | date nullable |
| `status` | announced / upcoming / effective / cancelled / corrected |
| `previous_state`, `new_state` | JSON 快照，仅作审计/展示，不替代关系表 |
| `aggregation_key` | unique stable key |
| `detected_at` | timestamptz |
| `source_evidence_id` | 主要证据 |
| `created_at`, `updated_at` | UTC |

### 5.4 `IndexChangeLeg`

保存聚合事件下的原子变化，使“Russell 2000 移除 + S&P 500 加入”既能作为一条偏移展示，又不丢失事实。

| 字段 | 说明 |
|---|---|
| `event_id` | FK IndexChangeEvent |
| `index_id` | FK MarketIndex |
| `membership_id` | FK IndexMembership nullable |
| `action` | ADDED / REMOVED / UPDATED / CANCELLED |
| `announcement_date`, `effective_date` | date nullable |
| `source_evidence_id` | 来源证据 |

唯一建议：`event + index + action + effective_date`。聚合窗口或规则变更时，不删除旧事件，而应标记 corrected 并重建新版本/审计关系。

## 6. 财报事件与日期变化

### 6.1 `EarningsEvent`

| 字段 | 说明 |
|---|---|
| `id` | UUID PK |
| `company_id` | FK Company |
| `fiscal_year` | integer |
| `fiscal_period` | Q1 / Q2 / Q3 / Q4 / FY / H1 / H2 / OTHER |
| `period_end_date` | date nullable |
| `estimated_release_at` | timestamptz nullable |
| `confirmed_release_at` | timestamptz nullable |
| `earnings_release_at` | timestamptz nullable |
| `conference_call_at` | timestamptz nullable |
| `filing_8k_at` | timestamptz nullable |
| `filing_periodic_at` | timestamptz nullable |
| `release_session` | pre_market / after_market / during_market / unknown |
| `status` | ESTIMATED / CONFIRMED / RELEASED / FILED / CANCELLED |
| `confidence` | 可解释等级或数值，算法待确认 |
| `primary_source_evidence_id` | 当前主证据 |
| `created_at`, `updated_at` | UTC |

候选唯一键：`company_id + fiscal_year + fiscal_period + period_end_date`。由于 `period_end_date` 可空、外国发行人和 Q4/FY 语义复杂，正式约束前需确定规范化规则；可先使用独立 `identity_key`，由同一规则版本稳定生成并唯一。

状态转换约束：FILED 不代表所有可能文件都齐全，而是满足产品定义的监管文件条件；具体条件待确认。取消后重新安排是恢复原事件还是新事件也待确认。

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
| `relation_type` | release_8k / periodic_report / foreign_report / other |
| `confidence` | 自动匹配置信度 |
| `match_rule_version` | 规则版本 |
| `review_status` | auto / confirmed / rejected |
| `created_at`, `reviewed_at`, `reviewed_by` | 审核信息 |

唯一约束：`filing + earnings_event + relation_type`。

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
| IndexMembership | company + index + 不重叠有效期 |
| IndexChangeEvent | aggregation_key unique |
| EarningsEvent | identity_key unique（规则待确认） |
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

1. 财报事件的最终唯一规则，尤其 Q4/FY、外国发行人、财年变更和未知 period end。
2. 只有“日期 + 盘前/盘后”时如何表达精度，以及从日期升级为具体时刻是否通知。
3. FILED 状态由首个 8-K、定期报告，还是两者满足何种组合触发。
4. 公司无 CIK、CIK 变更、ticker 重用、ADR/多上市身份的合并规则。
5. `/companies/{ticker}` 遇到历史 ticker 或跨交易所歧义时的行为。
6. 指数成分是否按证券还是按公司计算；同公司多个 share class 如何处理。
7. 指数偏移合并窗口、层级方向、多指数同时加入/退出的归类规则。
8. 来源可信度的量表、冲突胜出矩阵及人工修正是否锁定字段。
9. 用户级与公司级 ReminderRule 的覆盖/叠加规则。
10. 提醒“提前一天”按美东日期还是用户本地日期，以及夏令时边界。
11. 原始数据、通知内容、审计记录和已停用用户数据的保留期限。
12. polymorphic 来源/审计引用采用 Django ContentType 还是显式引用表；实现前需以查询和完整性测试做 ADR。
