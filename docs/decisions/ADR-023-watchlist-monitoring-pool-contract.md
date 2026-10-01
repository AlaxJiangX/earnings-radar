# ADR-023：Watchlist 与 Monitoring-Pool v2 Contract

- 状态：已接受（技术契约）；产品未决项见 §13
- 日期：2026-10-02
- 决策者：产品负责人
- 影响阶段：Stage 5.1A Watchlist Domain & Monitoring Status Recompute；Stage 5.1B Monitoring-Pool v2 Composition & Provider Compatibility
- 评审基线：`37953599482bb3eb0d5a9edca7620655f6e44cd5`（PR #65，Stage 4.5B merged-status）
- 关系：解决 ADR-012 §21 Open Decision #3；不改变 ADR-011 Option A、ADR-013 matching contract 或 ADR-020 frozen-basis 原则

## 1. 背景

Stage 5.1 的路线图交付是 `WatchlistItem`、普通/重点、自选开关和监控状态重算。真实仓库核对结果：

- `watchlists` app、模型、service、selector 均不存在；
- `Company.monitoring_status` 与 `monitoring_recalculated_at` 字段已存在，但没有重算 service；
- `companies.services.update_company` 当前允许直接修改 `monitoring_status`，会绕过未来派生规则；
- ADR-012 已冻结 v1 monitoring-pool selector：只接受 `enabled_index_codes`，selector version 与 hash contract 都硬编码为 v1；
- `MonitoringPoolSnapshot` / `MonitoringPoolMember` 已存在，`enabled_index_codes` 有非空 check constraint，`basis` 是 JSON；
- `resolve_monitoring_pool_snapshot_contract()` 目前按 v1 算法重算 hash，并严格要求每行 basis 的 v1 形状；
- 4.2F-A reference projection、4.2F-B Alpha Vantage resolution 和 4.2D-2 candidate matching 都从 persisted `MonitoringPoolMember.basis` 读取 `security_listing_id`；
- `watchlists` 当前不在 `INSTALLED_APPS`；审计 target choices 也不包含 `watchlist_item`。

本 ADR 冻结 Stage 5.1 的领域语义、selector v2 输入/输出、版本兼容、模块依赖和迁移边界。它不实现代码，也不修改 PRD。

## 2. 结论摘要

| 问题 | 决策 |
|---|---|
| Watchlist canonical unit | `Company`，不是 ticker / listing |
| Watchlist 生命周期 | `active` / `inactive`，由 `is_active` + `deactivated_at` 表达 |
| 普通 / 重点 | `priority_level = normal / important`，与生命周期正交，只影响排序与未来通知规则 |
| 单公司提醒开关 | `alerts_enabled`，与池成员资格正交；关闭提醒不退出监控池 |
| 同一用户重复添加 | `(user, company)` unique；active 重加为幂等 no-op |
| 删除 | 软删除；重新添加复用同一行；历史由 AuditRecord 保留 |
| 监控状态所有权 | `companies` 拥有字段与纯规则；通过显式 facts primitive 写入 |
| 自选触发的重算 | `watchlists` service 在同一事务内读取 indexes selector 并调用 companies primitive |
| 指数触发的重算 | 不在 5.1A 同步接线；由后续 pool-selection composition 统一重算 |
| Monitoring-Pool selector | 保留 v1；新增 `earnings-monitoring-pool-v2` |
| v2 watchlist 输入 | 调用方显式传入去重、可排序的 `watchlist_company_ids`；selector 不 import watchlists |
| v2 basis | 每个 member 至少一行 basis；listing basis 行保留 `security_listing_id`，无 listing 时使用 marker |
| Provider 投影 | watchlist 公司冻结 as-of listing 行；无 listing 时冻结 marker 行；三个消费方必须容忍 marker |
| Schema | 新增 `watchlists` app 表；audit 增加 `watchlist_item` target；不新增 earnings / companies 列 |
| 历史 as-of 重建 | MVP 不承诺；snapshot 只冻结选择时刻的输入 |
| 组合根 | earnings 命令/编排层可只读调用 watchlists 公开 selector；selector 本体不得 import watchlists |

## 3. 术语与权威边界

三层概念不得混用：

```text
Watchlist item（用户意图，per-user）
    active / inactive + priority_level + alerts_enabled

Company.monitoring_status（全局派生缓存）
    pending_identity / active / inactive

MonitoringPoolSnapshot（provider execution scope，全局 append-only）
    Company union + canonical basis + hash
```

规则：

- Watchlist item 只表达该用户是否关注该公司，不表达公司全局状态；
- `Company.monitoring_status` 是所有用户与指数事实聚合出的派生状态，不是 selector 输入；
- snapshot 是本轮同步应关注的 Company 集合，不保存 user 归属，也不替代 `monitoring_status`；
- 用户移除自己的自选不等于公司退出全局池：其他用户或指数仍可能维持该公司。

## 4. `WatchlistItem` 领域模型

### 4.1 字段

| 字段 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | 自选条目主键 |
| `user_id` | FK `AUTH_USER_MODEL`，`PROTECT`，related_name=`watchlist_items` | 资源所有者 |
| `company_id` | FK `companies.Company`，`PROTECT`，related_name=`watchlist_items` | 关注对象 |
| `priority_level` | `normal` / `important`，默认 `normal` | 与 lifecycle 正交 |
| `alerts_enabled` | boolean，默认 `true` | 单公司提醒总开关，不改变池成员资格 |
| `is_active` | boolean，默认 `true` | 生命周期 |
| `created_at` | timestamptz，auto_now_add | 首次添加时间，reactivation 不改写 |
| `updated_at` | timestamptz，auto_now | 最近属性变化 |
| `deactivated_at` | timestamptz nullable | 最近停用时间；reactivation 清空 |

### 4.2 约束

```text
UniqueConstraint(user, company)
    name = watchlists_item_user_company_unique

CheckConstraint(priority_level in ('normal', 'important'))
    name = watchlists_item_priority_valid

CheckConstraint(
    (is_active = true AND deactivated_at IS NULL)
    OR
    (is_active = false AND deactivated_at IS NOT NULL)
)
    name = watchlists_item_state_valid

Index(user, is_active)
Index(company, is_active)
```

### 4.3 状态与属性语义

```text
active:   is_active = true,  deactivated_at = null
inactive: is_active = false, deactivated_at != null
```

- 自选条目没有 `pending` 状态；`pending_identity` 属于 Company 身份维度；
- `priority_level` 与 `alerts_enabled` 不进入 lifecycle 状态机；
- 每个用户对同一 Company 只有一条主记录；
- 不物理删除。模型实例、QuerySet 和 Admin 不得提供通用 hard delete 路径；
- reactivation 复用原行，保留 `created_at`、`priority_level`、`alerts_enabled`，只改变
  `is_active` / `deactivated_at`；
- 单行 + reactivation 不能可靠重建历史 active intervals；历史以 AuditRecord 为准，
  MVP 不承诺 as-of watchlist 重建。

## 5. Watchlist Service Contract

### 5.1 公开用例

| 用例 | 输入 | 行为 |
|---|---|---|
| `add_watchlist_item` | `user`, `company_id`, `priority_level`, `as_of`, `reason`, `request_id`, 可选 IP | 首次创建 active 行；active 重加 no-op；inactive 重加复用并 reactivation |
| `remove_watchlist_item` | `user`, `item_id`, `reason`, `request_id`, 可选 IP | 软删除；inactive 重试 no-op |
| `set_watchlist_priority` | `user`, `item_id`, `priority_level`, context | 独立改级别；不隐式 reactivation |
| `set_watchlist_alerts` | `user`, `item_id`, `alerts_enabled`, context | 独立开关；不改变池成员资格 |

所有 mutation 必须：

- 从已认证 user 取得所有权，禁止从表单或 payload 接受 `user_id`；
- 以 `(id, user)` 或 `(company, user)` 查询；他人条目必须表现为 not found，不泄露存在性；
- 在数据库事务内完成领域写入与 AuditRecord 追加；
- 幂等重跑不产生第二条主记录、重复状态变化或重复 AuditRecord；
- 捕获 `(user, company)` 唯一冲突后回读既有行，不能依赖“先查后写”。

### 5.2 加入资格

`add_watchlist_item` 必须验证：

- `company_id` 存在；
- 在显式传入的 `as_of` 日期存在至少一个有效 `SecurityListing`：
  `effective_from <= as_of < effective_to-or-infinity`；
- `as_of` 必须是 `date`，不得在领域 service 内读取 wall clock。

MVP 不允许添加“当前没有有效 listing”的公司。listing 在添加后到期时，不自动删除自选；
该情形由 §8 的 marker 行处理。`priority_level` 只在首次创建时生效；既有行必须通过
`set_watchlist_priority` 显式改级，避免普通添加静默降级。

既有 active 条目的重复 add 是 no-op，不重新要求 listing 校验；inactive 条目的
reactivation 必须重新通过 as-of 有效 listing 校验。

“美国上市公司”的 canonical 判据当前不存在：SecurityListing 只有 exchange 字符串，
没有交易所国家策略。Stage 5.1A 只强制“存在有效 listing”；US 资格定义见 §13。

### 5.3 Audit

- 所有四个 mutation 通过 `audit.services.record_user_action` 追加 AuditRecord；
- target type 新增 `watchlist_item`，target_id 为条目 UUID；
- `before` / `after` 只包含 `company_id`、`priority_level`、`alerts_enabled`、
  `is_active`、`deactivated_at`；不得包含 email、密码、session 或 token；
- action 映射：首次创建 `create`；reactivation / priority / alerts 变化 `update`；
  软删除 `deactivate`；
- 不写 DataChange：WatchlistItem 是用户偏好，不是外部来源标准化事实；
- 相同 request_id 重放复用同一 AuditRecord；无状态变化的 no-op 不新增 AuditRecord。

### 5.4 Selectors

```text
get_watchlist_items_for_user(user)
    返回当前 user 的条目；不得隐式返回其他 user 数据

get_active_watchlist_company_ids(*, company_ids=None)
    返回全体 user 的 active、去重、按 UUID 字符串升序的 Company UUID tuple；
    不返回 user 归属，供 pool-selection composition 使用
```

除内部 composition 外，页面与 API 只能使用带 `user` 过滤的查询。

## 6. Monitoring Status Recompute Contract

### 6.1 规则

```text
if caller 明确声明 identity_pending:
    monitoring_status = pending_identity
elif has_enabled_index_membership OR has_active_watchlist:
    monitoring_status = active
else:
    monitoring_status = inactive
```

规则只由 `companies.services` 的公开写入原语执行。任何 caller 都必须传入显式 facts：

```text
has_enabled_index_membership: bool
has_active_watchlist: bool
identity_pending: bool
as_of: date
```

`monitoring_status` 与 `monitoring_recalculated_at` 只能由该原语变更；
`companies.services.update_company` 必须拒绝 `monitoring_status` 变更，防止通用更新绕过重算。
Company Admin 已是全只读，不提供旁路。

公开 service 签名：

```python
recalculate_company_monitoring_status(
    *,
    company_id: UUID,
    as_of: date,
    has_enabled_index_membership: bool,
    has_active_watchlist: bool,
    identity_pending: bool,
    actor_user: User | None = None,
    sync_run: SyncRun | None = None,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> CompanyMonitoringStatusResult
```

context 规则与 `companies` 现有写入口一致：至少提供 `actor_user` 或 `sync_run`；
提供 `actor_user` 时必须同时提供非空 `reason` 与 `request_id`，系统重算必须提供
`sync_run` 与稳定 request identity。

### 6.2 写入语义

- 原语使用 `select_for_update()` 锁 Company 行；
- `monitoring_status` 或 `monitoring_recalculated_at` 实际变化时，通过
  `audit.services.record_data_change` 追加对应 DataChange；
- DataChange 的 `rule_version` 固定为 `monitoring-status-recompute-v1`；
- 每次成功的重算追加 Company AuditRecord，并更新 `monitoring_recalculated_at`；
- 相同状态重跑仍可更新 recalculated_at，但不得产生重复领域语义变化；
- facts 非法、Company 不存在或 context 不完整时必须 fail closed。

### 6.3 自选触发路径

`watchlists.services` 在 add / remove / reactivation 的同一事务内：

```text
has_active_watchlist
    = WatchlistItem.objects.filter(company=..., is_active=True).exists()

has_enabled_index_membership
    = indexes.selectors.company_indexes_as_of(
          company_id=..., as_of=..., is_enabled=True
      ).exists()

identity_pending
    = caller 传入的显式事实（定义见 §13 PD-1）
```

随后调用 `companies.services` 重算原语。priority / alerts 变化不触发重算。

该路径需要 `watchlists -> indexes.selectors` 只读依赖；`indexes` 不得反向 import
`watchlists`，因此指数成员变化不在 5.1A 内同步重算。5.1B / 生产激活前必须由 pool-selection
composition 对所有受影响 Company 执行同一重算原语，消除缓存滞后。

### 6.4 `pending_identity`

`pending_identity` 的“什么算身份未决”在仓库中没有权威定义。本 ADR 冻结其优先级：
identity_pending 为真时覆盖 active/inactive 判定；但事实判定规则保持未决（PD-1）。
在 PD-1 关闭前，任何把 `identity_pending` 事实固定映射为 CIK 空 / listing 缺失的代码
都不得进入实现。

## 7. Monitoring-Pool v2 Selector Contract

### 7.1 版本常量

```text
selector v1 = earnings-monitoring-pool-v1
selector v2 = earnings-monitoring-pool-v2
hash v1     = earnings-monitoring-pool-hash-v1
hash v2     = earnings-monitoring-pool-hash-v2
input v2    = earnings-monitoring-pool-input-v2
```

v1 常量、算法、basis 形状和 hash 必须保持字节级兼容；v2 通过版本分派实现，不得改写
既有 snapshot。

### 7.2 公开入口

```python
select_monitoring_pool(
    *,
    as_of: date,
    selector_version: str,
    enabled_index_codes: Iterable[str],
    watchlist_company_ids: Iterable[UUID] | None = None,
) -> MonitoringPoolSelectionResult
```

约束：

- v1：`watchlist_company_ids` MUST be `None`，否则拒绝；
- v2：`watchlist_company_ids` MUST be provided，allow empty iterable；
- 未知 selector version：typed selector error，fail closed；
- `watchlist_company_ids` 接受 UUID 集合并做去重、排序；未知 Company UUID 拒绝；
- selector 本体不得 import `watchlists` 模型、selector 或 service。

结果对象新增只读 canonical `watchlist_company_ids`（v1 返回空 tuple）；既有 v1 属性与
返回语义不变。

### 7.3 成员并集

```text
member(company)
    = as-of normative enabled-index membership
      UNION
      active watchlist company
```

- 同一 Company 在多个 index、多个 listing 或多个 watchlist user 命中时只产生一条 member；
- `priority_level`、`alerts_enabled` 和 user 数量不改变 member set 或 hash；
- 多用户导致同一 Company 集合不变时，snapshot 复用；
- 某个 user 移除但其他 user 仍 active 时，member set 不变。

### 7.4 v2 basis 形状

每个 member 至少一行 basis。listing basis 行（index 或 watchlist listing）必须包含
`security_listing_id`、`effective_from`、`effective_to`（`effective_to` 可为 null），
以满足现有 provider 投影的硬校验；marker 行是唯一例外，只包含 `source`。

Index basis row：

```json
{
  "source": "index",
  "index_code": "SP500",
  "security_listing_id": "<uuid>",
  "effective_from": "YYYY-MM-DD",
  "effective_to": null
}
```

`effective_from` / `effective_to` 是 `IndexMembership` 的 as-of 半开区间，含义与 v1 相同。

Watchlist listing basis row：

```json
{
  "source": "watchlist",
  "security_listing_id": "<uuid>",
  "effective_from": "YYYY-MM-DD",
  "effective_to": null
}
```

`effective_from` / `effective_to` 是 `SecurityListing` 的半开有效期，不是 membership 区间。
selector 在传入的 `as_of` 解析这些 listing 事实；caller 只传 Company UUID，不传 listing id。

Watchlist marker row（仅当该公司在 as_of 没有有效 listing）：

```json
{
  "source": "watchlist"
}
```

marker 行表达“用户仍在关注，但没有可执行的 provider symbol”。不得为 marker 行伪造
listing id 或 temporal facts。

### 7.5 v2 input revision

输入 manifest 的 canonical JSON：

```json
{
  "contract": "earnings-monitoring-pool-input-v2",
  "enabled_index_codes": ["DJIA", "NASDAQ100", "SP500"],
  "index_memberships": [
    {
      "company_id": "<uuid>",
      "index_code": "SP500",
      "security_listing_id": "<uuid>",
      "membership_effective_from": "YYYY-MM-DD",
      "membership_effective_to": null,
      "listing_effective_from": "YYYY-MM-DD",
      "listing_effective_to": null
    }
  ],
  "watchlist_company_ids": ["<uuid>"],
  "watchlist_listings": [
    {
      "company_id": "<uuid>",
      "security_listing_id": "<uuid>",
      "listing_effective_from": "YYYY-MM-DD",
      "listing_effective_to": null
    }
  ]
}
```

规则：

- `enabled_index_codes` 去重、升序；
- `index_memberships` 与 `watchlist_listings` 使用 canonical sort，见 §7.7；
- `watchlist_listings` 包含每个 active watchlist 公司在 as_of 的全部有效 listing；
- 无 listing 的 watchlist 公司不出现在 `watchlist_listings`，但出现在
  `watchlist_company_ids`；
- `input_revision` = SHA-256(canonical JSON)；编码规则与现有 `_sha256_json` 相同：
  `sort_keys=True, separators=(",", ":"), ensure_ascii=True`；
- listing 事实变化必须改变 input revision，不能只改变 basis 而复用旧 revision。

### 7.6 v2 pool hash

```json
{
  "contract": "earnings-monitoring-pool-hash-v2",
  "selector_version": "earnings-monitoring-pool-v2",
  "as_of": "YYYY-MM-DD",
  "enabled_index_codes": ["DJIA", "NASDAQ100", "SP500"],
  "input_revision": "<sha256>",
  "members": [
    {
      "company_id": "<uuid>",
      "basis": [
        {
          "source": "index",
          "index_code": "SP500",
          "security_listing_id": "<uuid>",
          "effective_from": "YYYY-MM-DD",
          "effective_to": null
        }
      ]
    }
  ]
}
```

`pool_hash` = SHA-256(canonical JSON)。

### 7.7 Canonical ordering

- members 按 `str(company_id)` 升序，ordinal 从 0 连续；
- 同一 member 的 basis 顺序：index rows → watchlist listing rows → watchlist marker；
- index rows 按 `(index_code, security_listing_id, effective_from, effective_to)` 排序；
- watchlist listing rows 按
  `(security_listing_id, effective_from, effective_to)` 排序；
- marker 只能单独出现（member 有 marker 时不得同时有 listing rows）；
- 重复 basis 行必须去重后写入；persisted 非 canonical 顺序必须 fail closed。

### 7.8 Snapshot persistence

复用现有 schema：

```text
MonitoringPoolSnapshot
    as_of_date
    selector_version = earnings-monitoring-pool-v2
    enabled_index_codes
    input_revision
    pool_hash
    member_count

MonitoringPoolMember
    snapshot
    company
    ordinal
    basis
```

不新增列、不迁移 earnings 表。v2 snapshot 与 v1 snapshot 通过 `selector_version` 隔离；
两个唯一约束保持不变。`enabled_index_codes` 非空约束继续生效：MVP 至少启用一个指数；
零指数模式见 §13。

### 7.9 Resolver 版本分派

`resolve_monitoring_pool_snapshot_contract(as_of, selector_version, pool_hash)`：

```text
selector_version = v1
    -> 使用现有 v1 canonicalization / input revision / hash，行为不变

selector_version = v2
    -> 使用 §7.4-§7.7 重建 manifest、input revision、members 和 hash；
       任何 shape、来源、区间、排序或 hash mismatch 都 fail closed

其他
    -> MonitoringPoolIntegrityError
```

v1 snapshot 在 v2 上线后必须继续可解析，否则会破坏既有 SyncRun 的 retry / offline replay
（ADR-011 Option A）。禁止回填或重写 v1 snapshot。

### 7.10 隐私与用户归属

Snapshot、member basis、pool hash、input revision 和日志都不得包含：

```text
user_id
email
watchlist item id
priority_level
alerts_enabled
```

用户集合只通过 `watchlist_company_ids` 影响 Company 并集；同一 Company 集合不因
“谁关注了它”而改变 hash。

## 8. Provider Projection Compatibility

现有 snapshot basis 消费方在 v2 激活前必须兼容 marker 行。要求：

| consumer | 要求 |
|---|---|
| `reference_calendar_projection._match_rows` | 跳过无 `security_listing_id` 的 marker；不得整轮 fail |
| `alpha_vantage_canonical._basis_company_by_listing` | 跳过 marker；继续映射 listing rows |
| `candidate_matching._validated_basis` / `_load_pool` | 跳过 marker；member 无有效 listing 时跳过该 member 并记录诊断，不得让整轮失败 |

规则：

- listing rows 继续使用 `security_listing_id + listing effective interval` 解析 symbol；
- marker-only member 不产生 provider symbol，不参与 provider query，但仍是 pool member；
- 跳过必须有 run-level diagnostic / counter，不能静默吞掉；
- replay 只使用 persisted snapshot basis，不读取 current listing、不重选 pool；
- 在 §8 兼容改动进入同一变更前，v2 snapshot MUST NOT 进入上述 provider 路径。

## 9. 模块边界与依赖

本 ADR 明确修订 architecture 的依赖表：

```text
watchlists -> accounts
           -> companies
           -> indexes.selectors        （新增，只读）
           -> audit.services           （新增，追加审计）

earnings   -> companies, indexes, filings, providers, audit
           -> watchlists.selectors     （新增；仅命令/编排层）
```

禁止：

- `earnings.services.monitoring_pool`、models、selectors 或 provider code import `watchlists`；
  只有 earnings 的 management command / 显式 orchestration 模块可以调用
  `watchlists.selectors`；
- `watchlists` import `earnings`、`notifications`、`filings` 或 `providers`；
- `indexes` import `watchlists`；
- `companies` import `indexes`、`watchlists` 或 `earnings`；
- 把 user 归属写入 snapshot、basis、SyncRun scope 或 provider request。

Pool-selection composition 的职责：

1. 调用 `watchlists.selectors.get_active_watchlist_company_ids()`；
2. 把去重后的 Company UUID 集合显式传给 v2 selector；
3. 在调用 Provider 前把 selector 返回的 `as_of_date`、`selector_version`、`pool_hash`
   写入 SyncRun scope；
4. 不重新评估 watchlist、不写入 watchlist 表、不修改 selector 内部数据。

该 composition 尚未实现；在它落地前，v2 selector 只允许测试调用，生产 scheduled path
不得声称已使用 v2。

## 10. Schema 与 Migration

| 变更 | 内容 |
|---|---|
| 新增 app | `watchlists.apps.WatchlistsConfig` 加入 `INSTALLED_APPS` |
| `watchlists/migrations/0001_initial.py` | `WatchlistItem` 表、两个 FK、unique、两个 check、两个 index |
| `audit/migrations/0013_*` | AuditRecord target choices 增加 `watchlist_item`，重建 `audit_record_target_type_valid` |
| earnings schema | 无 migration；复用 JSON basis + selector version |
| companies schema | 无 migration；只新增 service 与 service 限制 |
| notifications | Stage 5.1 不创建 |

Audit 迁移只改 `AuditRecord` 的 choices / check；不得把 `watchlist_item` 加入
`DomainTargetType`、DataChange 或 SourceEvidence 的允许 target。

## 11. 现有代码核对

| 需求 | 现状 | 缺口 / 结论 |
|---|---|---|
| 用户与公司 FK | 已有自定义 User、Company、SecurityListing | 可复用 |
| `(user, company)` 唯一 | 无 | 新模型 + DB 约束 |
| 软删除 / reactivation | 无 | 新字段 + service |
| `alerts_enabled` | data-model 已规划 | 新模型字段 |
| `monitoring_status` 字段 | 已有，Admin 只读 | 无重算 service；`update_company` 仍可直接改 |
| 指数 as-of 事实 | `indexes.selectors.company_indexes_as_of` 已有 | 新重算编排接入 |
| watchlist as-of 事实 | 无 | 新 selector；当前态无历史区间 |
| v1 selector | 已实现并冻结 | 保留不动 |
| v2 input / hash / resolver | 无 | 本 ADR 冻结实现契约 |
| basis 消费方 | reference / AV / matching 硬要求 listing id | 必须容忍 marker；激活门 |
| SyncRun scope | 已携带 selector_version / pool_hash | 无 schema 变更 |
| 审计 | 追加式 AuditRecord + IP hash 已有 | 需新增 watchlist_item target |
| 通知取消 | notifications app 不存在 | 留 Stage 6 |
| 历史 as-of 重建 | 单行 + AuditRecord | MVP 不承诺 |

## 12. Stage 5.1 拆分与验收

### 12.1 Stage 5.1A — Watchlist Domain & Monitoring Status Recompute

交付：

- `watchlists` app、WatchlistItem、migration；
- add / remove / priority / alerts service，全部 user-scoped、幂等、审计；
- `companies.services` 重算原语 + `update_company` 关闭 monitoring_status 旁路；
- `watchlists -> indexes.selectors` 只读重算路径；
- v1 selector 回归不变；v2 selector / resolver / persistence contract；
- import-boundary、权限、并发、幂等、审计测试。

验收：

- 同一用户重复添加不产生第二条记录；
- active 重加 no-op；inactive 重加复用原行并保留原级别；
- remove 软删除且重跑幂等；
- 跨用户读写拒绝；
- priority / alerts 不改变 pool member set；
- 重算 truth table 与事务回滚测试通过；
- v1 既有测试全部通过，v1 hash/basis 不变；
- 现有 `test_unknown_selector_version_is_rejected` 以
  `earnings-monitoring-pool-v2` 作为 unknown 值；实现 v2 时必须把该测试的 unknown 值改为
  真正的未知版本（如 v3），断言语义保持不变；
- v2 输入顺序、重复、未知 Company、marker、listing 变化、snapshot 复用测试通过；
- snapshot / basis 不含 user 归属。

### 12.2 Stage 5.1B — Monitoring-Pool v2 Composition & Provider Compatibility

前置：Stage 5.1A 完成；存在或同时实现 calendar command / orchestration caller。

交付：

- earnings composition 只读调用 watchlists selector，显式传入 v2 input；
- SyncRun scope 持久化 v2 contract；
- reference / AV / candidate matching 的 marker 兼容；
- skipped-member diagnostics；
- retry / replay 使用 persisted contract，不重选 pool、不重读 current watchlist。

验收：

- 同一 watchlist Company 集合 + 同一指数输入 → 同一 snapshot；
- listing 变化 → 新 input revision / snapshot；
- v2 snapshot 的 provider 路径不因 marker 行失败；
- 无 listing 的 watchlist 公司产生可观测诊断；
- 全部 retry / replay 测试通过。

## 13. Open Decisions / Deferred

以下不是本 ADR 的技术阻塞项，但不得在对应实现中自行猜测：

1. `OPEN DECISION PD-1`：什么算 `identity_pending`（CIK 空、无有效 listing，或其他规则）；
2. `OPEN DECISION PD-2`：US-listing 的 canonical 判据（exchange allowlist / 国家策略）；
3. `OPEN DECISION PD-3`：watchlist 数量上限与滥用防护，最晚公开注册（Stage 8.4）前确认；
4. `DEFERRED PD-4`：移除自选时是否立即取消待发送通知，Stage 6.1 / 6.2 决定；
5. `DEFERRED PD-5`：全部指数 disabled、只靠 watchlist 的零指数模式；MVP 保持
   `enabled_index_codes` 非空；支持该模式需要同时放开 selector 非空校验并迁移现有
   check constraint；
6. `DEFERRED PD-6`：账号删除、匿名化和数据保留期限，最晚 Stage 8.1；
7. `DEFERRED PD-7`：历史 as-of watchlist 重建；若未来需要，必须新增 append-only
   active interval 模型，不能从当前单行推导；
8. `DEFERRED`：自选分组、备注/标签、CSV 导入导出、共享自选、per-company manual
   include/exclude、Telegram / Web Push / PWA。

## 14. Gate Decision

```text
PASS — Stage 5.1 contract is frozen
```

理由：

- Watchlist lifecycle、priority、alerts、delete / reactivation 与唯一约束已冻结；
- monitoring status 的 facts primitive 与事务边界已冻结；
- v1 selector / hash 的 byte-level 兼容已冻结；
- v2 input、basis、input revision、pool hash、resolver 分派已冻结；
- provider projection 的 marker 兼容要求与激活门已冻结；
- module dependency 修订为无环只读边；
- schema / migration 范围已列出；
- 产品未决项已显式隔离，未冒充已确认规则。

下一阶段：

```text
Stage 5.1A — Watchlist Domain & Monitoring Status Recompute
```

Stage 5.1B 只在 calendar command / orchestration caller 存在或同时实现时开始；不得把
v2 snapshot 提前接入生产 provider 路径。
