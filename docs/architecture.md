# Earnings Radar 技术架构

> 状态：规划稿（基于 PRD v0.1）
>
> 范围：MVP 技术规划；不代表已完成实现
> 需求来源：`docs/product-requirements.md`（由本次提供的 PRD v0.1 附件原样复制，未改写内容）。

## 1. 架构目标与边界

Earnings Radar 采用 Django 模块化单体。一个代码仓库、一个 Django 部署单元和一个 PostgreSQL 数据库承载 Web 页面、管理后台、数据同步和通知发送；不同业务能力通过 Django app、服务层和数据库约束隔离，而不是拆成微服务。

MVP 的架构目标是：

- 稳定覆盖四个基础指数及用户自选股中的美国上市公司；
- 将财报、SEC 文件、指数变化和通知组织为可追溯的业务记录；
- 所有外部数据通过 Provider 适配器进入系统；
- 所有同步与发送任务可安全重跑，不制造重复数据或重复通知；
- 关键字段保留来源、原始响应和变更历史；
- 本地可用 Docker Compose 启动，生产初期仅依赖 Web Service、PostgreSQL、Cron Job 和邮件服务；
- 页面以 Django Templates 为主，HTMX 只用于局部筛选、分页和轻量交互。

MVP 明确不引入 Celery、Redis、微服务、SPA 前端、实时行情、AI 投资建议、市场热门和 Reddit 热门。市场热门、Reddit、Telegram、Web Push、PWA 和自选股分组属于第二阶段，不能成为 MVP 上线前置条件。

## 2. 系统上下文

```text
访客 / 注册用户 / 管理员
            |
            v
      Django Web Service ------------------> 邮件服务
       | Templates + HTMX                       ^
       | Django Admin                           |
       v                                        |
       PostgreSQL <----------------------- Cron commands
            ^                                   |
            |                                   v
            +------ Provider adapters ------ 外部数据源
                    - 财报日历
                    - 公司 IR
                    - SEC EDGAR
                    - 指数来源
```

Web 请求不直接访问外部数据源。Cron Job 调用 Django management command，Provider 先保存原始数据，再由领域服务完成标准化、核对、变更检测和通知入队。

## 3. 模块划分

| Django app | MVP 职责 | 允许依赖 | 不负责 |
|---|---|---|---|
| `accounts` | 自定义用户、认证、时区和用户偏好 | Django auth | 公司及事件业务 |
| `companies` | 公司、CIK、股票代码/上市身份、公司搜索、监控状态 | `audit` 的来源引用接口 | 直接抓取第三方数据 |
| `indexes` | 证券级指数成分历史、加入/移除、偏移聚合 | `companies`, `providers`, `audit` | 用户通知投递 |
| `earnings` | 财报事件生命周期、日期变更、来源核对 | `companies`, `filings`, `providers`, `audit` | 外部 HTTP 细节 |
| `filings` | SEC 文件、财报关联、公开列表 | `companies`, `providers`, `audit` | 复制 SEC 全文 |
| `watchlists` | 自选股、普通/重点关注、公司级提醒开关 | `accounts`, `companies` | 全局通知策略 |
| `notifications` | 提醒规则、通知生成、站内通知、邮件投递和重试 | 各领域的稳定公开接口 | 判断外部数据真实性 |
| `providers` | Provider 协议、HTTP 客户端、标准化 DTO、同步编排入口 | `audit` | 页面渲染和用户权限 |
| `audit` | 数据来源、同步运行、原始数据、字段来源、变更及操作审计 | 尽量不反向依赖业务 app | 修改领域状态 |

第二阶段再启用 `market_trends` 和 `social_signals`。它们只能读取当前监控池和公司主数据，不应侵入 MVP 的财报、SEC 或通知模型。

### 3.1 模块内部结构

每个业务 app 使用相同的职责分层：

```text
models.py / models/     数据结构、约束和最小领域不变量
services/               用例、事务边界、核对和状态转换
selectors/              只读查询，供页面和其他模块使用
views.py / views/       HTTP 参数校验、权限、调用服务、渲染
templates/              服务端页面与 HTMX partial
admin.py                 受权限控制的运维入口
tests/                   单元、服务、集成和页面测试
```

Provider 适配器不得写入业务表；它返回标准化结果，并通过同步服务统一落库。View 和模板不得包含状态转换、优先级计算或外部请求。

## 4. Provider 适配器

### 4.1 统一契约

每个 Provider 至少暴露以下概念，而非要求所有供应商使用相同 HTTP API：

- Provider 身份、版本和能力；
- 请求范围与游标；
- 明确的连接和读取超时；
- 限速及重试分类；
- 原始响应（正文或对象存储引用；MVP 可先存 PostgreSQL）；
- 内容类型、抓取时间、来源 URL、HTTP 元数据和内容哈希；
- 标准化结果及逐字段来源；
- 可区分的临时错误、永久错误和数据校验错误。

MVP Provider 类型：

1. `EarningsCalendarProvider`：未来预计财报和初步发布时间段；
2. `InvestorRelationsProvider`：有限重点公司范围内的官方确认、电话会和新闻稿；
3. `SecEdgarProvider`：CIK 映射及指定表单的最新提交；
4. `IndexConstituentProvider`：指数快照、公告日期和生效日期（具体来源待确认）。

### 4.2 数据进入流水线

```text
抓取
  -> 保存 RawDataRecord（先保存、不可原地覆盖）
  -> 校验与标准化
  -> 生成字段观察值 / 来源证据
  -> 领域核对服务选择当前值
  -> 同事务写入当前记录与变更历史
  -> 创建待发送通知
```

来源优先级原则是“官方且直接的证据优先”，但具体冲突矩阵必须配置并测试，不能只写死为某供应商优先。建议的默认顺序为公司 IR/SEC 官方文件高于第三方预计数据；该顺序仍需产品负责人确认其业务细节。

### 4.3 原始数据保留

- 原始数据与标准化业务表分离；
- 以 `source + request_fingerprint + content_hash` 去重相同响应；
- 每次同步运行仍记录“已获取但内容未变”的统计，不重复保存相同正文；
- 原始记录不可原地修改；解析器升级时可从原始记录重放并产生新的解析版本；
- SEC MVP 只保留元数据、链接和必要的解析证据，不复制全量文件正文；
- 原始数据保留期限和容量上限待确认。

## 5. 领域流程

### 5.1 公司与监控池

CIK 是发行人监管身份，股票代码是可变的上市身份。公司主记录按 CIK 优先去重；股票代码必须支持交易所、有效期和主代码标记，不能把 ticker 当永久唯一标识。

IndexMembership 绑定 `SecurityListing`，而不是直接绑定 Company。每个 listing 表达具体 ticker、交易所和 share class，并保留有效期；Company 层面的指数归属通过其全部有效 listing 聚合。因此，同一公司可凭不同 share class 同时拥有不同指数身份，历史 ticker 也不会被当前 ticker 覆盖。

`monitoring_status` 是公司级、可重算的派生状态：只要公司的任一有效 SecurityListing 属于任一启用指数，或公司存在任一有效用户自选股，该公司即为启用。指数或自选股变化后，在同一事务中重新计算。多个 listing 命中同一指数只计为一个公司级归属展示，但底层成员关系全部保留。退出监控池只停止未来同步和普通提醒，历史记录不删除。该决策见 `docs/decisions/ADR-002-index-migration-rules.md`。

### 5.2 指数同步与偏移

每次指数同步先把来源中的成分解析到具体 SecurityListing，保存完整或可验证的来源快照，再与某一生效时间点的有效证券级成分关系比较：

1. IndexChangeLeg 只保存 `ADDED` 或 `REMOVED` 原子事实；
2. 同一公司加入/移除生效日期相同则自动合并偏移；相差 1–7 个自然日形成待复核候选；超过 7 日保持独立；
3. `movement_direction` 仅按以下规则计算：Russell 2000 → 任一大型指数为 `UPGRADE`，任一大型指数 → Russell 2000 为 `DOWNGRADE`，S&P 500/Nasdaq 100/Dow 30 之间为 `CROSS_INDEX`，其他为 `NONE`；
4. 聚合事件根据公司全部有效 listing 和历史计算 `monitoring_impact`：`CONTINUES`、`ENTERS_BASE_POOL`、`EXITS_BASE_POOL` 或 `REENTERS_BASE_POOL`；REENTERS 仅用于历史上曾退出后重新进入；
5. 修正与取消是事件状态/版本变化，不与原子动作、偏移方向或监控影响混在同一枚举；
6. 多 share class 同时变化时逐条保留 leg，再按公司聚合展示。

以上窗口和方向规则已经由 ADR-002 确定。聚合事件不能替代底层证券级成分关系历史，1–7 日候选也不得在人工确认前触发正式偏移通知。

### 5.3 财报事件生命周期

财报正式身份由 `company_id + period_end_date + period_type` 确定，并保存稳定的 `identity_key` 与 `identity_rule_version`。年度财报内部统一为 `period_type=FY` 且 `includes_q4=true`，不另建 Q4 正式事件。52/53 周财年使用 `fiscal_calendar_type` 和 `period_length_weeks` 表达，不扩张 period_type。财年标签是展示/来源属性，不参与正式唯一身份。`period_end_date` 未知时只建立候选事件，候选记录必须依赖来源事件标识并等待核对，不能用 `company + fiscal_year + fiscal_period` 冒充正式唯一键。该决策见 `docs/decisions/ADR-001-earnings-event-identity.md`。

财报发布生命周期只使用 `SCHEDULED_ESTIMATED`、`SCHEDULED_CONFIRMED`、`RELEASED` 和 `CANCELLED`。晚到数据不得无审计地使状态倒退；管理员修正必须写原因和审计记录。预计、确认、实际发布和电话会时间分别保存。

SEC Filing 不属于 EarningsEvent 的单向状态机。`Filing` 保存每份监管文件，`FilingEarningsLink` 保存它与财报事件的关系。Release filing 使用 `YES`、`NO`、`REVIEW_REQUIRED` 三态分类，并保存分类原因与规则版本；只有 YES 推导 `has_release_filing=true`。`has_periodic_filing` 独立推导。页面因此可以同时展示“财报已发布、8-K 已提交、10-Q 待提交”，也能表达外国发行人的 6-K/20-F/40-F 和不同提交顺序。该决策见 `docs/decisions/ADR-003-release-filing-classification.md`。

任何影响用户理解的日期或状态变化都写入 `EarningsDateChange`/通用变更历史；只有值确实变化才生成记录和通知。相同数据重复同步只增加运行统计，不生成新变更。

### 5.4 SEC 文件

按 SEC accession number 全局去重。文件先关联公司，再通过报告期、表单类型和时间窗口关联一个或多个财报事件。自动关联必须保存规则版本和置信度；不确定关联可由管理员复核，不能静默覆盖。release filing 与 periodic filing 的可用性分别从已确认的 FilingEarningsLink 推导，任何一类文件都不推动 EarningsEvent.status。

### 5.5 通知

第一阶段以 PostgreSQL 表作为持久通知队列，不引入消息代理：

1. 领域服务产生变化后，根据有效提醒规则创建 Notification；
2. 唯一 `idempotency_key` 防止同一用户、规则、事件版本和渠道重复入队；
3. 每 5 分钟的命令使用 `select for update skip locked`（或等价锁）领取批次；
4. 邮件发送结果写入独立投递尝试；临时失败按有限退避重试；
5. 站内通知入队即形成可读记录，阅读状态与投递状态分离；
6. 摘要使用稳定的时间桶键，重跑不得重复发送同一摘要。

事务提交前不调用邮件服务。若“外部邮件已接受，但本地写结果失败”，重试可能造成供应商侧重复，因此应向邮件服务传递稳定幂等键（若供应商支持）；不支持时记录这一残余风险。

## 6. 调度、并发与幂等

MVP 使用 Django management commands 作为所有后台入口。建议生产调度：

| 入口 | 目标频率 | 说明 |
|---|---:|---|
| SEC 同步 | 5–15 分钟 | 具体频率服从 SEC 访问政策和部署能力 |
| 通知发送 | 5 分钟 | 可与轻量调度命令合并，必须有运行锁 |
| IR 确认 | 6 小时或每天多次 | 仅有限公司/Provider |
| 财报日历 | 每天 | 按监控池分批 |
| 指数成分 | 每天 | 保留快照并比较 |
| 健康检查 | 每天 | 生成管理员可见告警/日志 |

每类任务以 `SyncRun` 记录开始、结束、计数和错误；使用 PostgreSQL advisory lock 或任务锁表防止同类任务重叠。任务按稳定业务键 upsert，并在数据库层设置唯一约束。外部请求重试只覆盖明确的临时故障，带超时、最大次数和抖动退避。

生产环境可配置多个 Cron Job，也可用一个每 5 分钟运行的短生命周期 dispatcher 检查哪些任务到期并执行；最终方案取决于托管平台对计划任务频率、并发和最长运行时间的限制，部署前必须验证。不得在 Web 进程内启常驻 scheduler。

### 6.1 数据新鲜度目标

新鲜度从上游可观察时间起算，到 Earnings Radar 中对应标准化记录可查询或通知被邮件供应商接受为止。下表是已确认的首版内部工程目标，不是对外 SLA；统计口径与发布门槛见 ADR-004。

| 数据/动作 | 建议目标 | 起点与终点 | 状态 |
|---|---:|---|---|
| SEC 文件发现 | 15 分钟内 | SEC `accepted_at` 至 Filing 可查询 | 初始目标 |
| 财报日历更新 | 上游变化后 24 小时内 | Provider 可见变化至 EarningsEvent 更新 | 初始目标 |
| IR 官方确认更新 | 官方页面发布后 6 小时内 | IR 公告可见至确认状态更新 | 初始目标 |
| 指数公告更新 | 官方公告后 24 小时内 | 公告发布至 IndexChangeEvent 可查询 | 初始目标 |
| P0/P1 通知发送延迟 | 事件入库后 5 分钟内 | 领域变化提交至邮件供应商接受/站内通知可见 | 初始目标 |

每项目标都要记录 `source_observed_at`、`fetched_at`、`normalized_at` 和需要时的 `notification_sent_at`，以区分上游延迟、同步延迟和发送延迟。数据源细节与测量方法见 `docs/data-sources.md`。

## 7. Web、权限与页面策略

- 公开页面：`/`、`/earnings`、`/companies`、公司详情、`/index-changes` 及公开 SEC/来源信息；
- 登录页面：`/dashboard`、`/watchlist`、`/notifications`、`/settings/notifications`；
- 所有用户资源查询必须以当前用户过滤，不能只依赖前端隐藏；
- Django Templates 输出完整页面；HTMX endpoint 返回明确的 partial，并支持普通请求回退；
- 搜索、筛选和分页参数在服务端校验；
- POST/PUT/DELETE 类交互使用 CSRF；
- Django Admin 仅给授权人员，手工修正必须要求原因并留下审计；
- 用户时区仅用于展示和提醒窗口计算，数据库 `DateTime` 一律保存 UTC。

## 8. 数据一致性与事务

- 当前状态、对应变更记录和通知入队应尽可能处于同一数据库事务；
- 唯一约束是幂等的最后防线，应用层先查询不能替代数据库约束；
- 日期型业务事实（如仅有公告日、生效日）用 `date`；具体时刻用 UTC `timestamptz`；
- 删除公司、来源、事件等关键对象原则上使用停用/结束有效期，不做级联物理删除；
- 跨模块引用使用稳定主键和公开服务/selector，禁止循环 import 私有实现；
- 当前值可以修正，但旧值、新值、来源、任务和操作者必须可追溯。

详细实体和约束见 `docs/data-model.md`。

## 9. 本地与生产拓扑

### 9.1 本地

Docker Compose 规划包含：

- `web`：Django 开发服务/测试命令；
- `db`：PostgreSQL；
- 可选的一次性 `job` profile：手工执行同步命令。

MVP 不添加 Redis 或 Celery 容器。密钥从未提交的 `.env` 注入，仓库仅提供 `.env.example`（在后续实现阶段创建）。

### 9.2 生产初期

- 一个 Web Service；
- 一个托管 PostgreSQL；
- 一个或多个短生命周期 Cron Job；
- 一个第三方邮件服务；
- HTTPS、环境变量、数据库备份和迁移发布步骤。

发布时先运行迁移，再切换 Web；同步命令必须兼容滚动发布期间的前后一个 schema 版本，或在维护窗口暂停 Cron。备份恢复演练和数据库容量告警属于上线验收，而不仅是配置项。

## 10. 测试与 CI 规划

后续实现阶段的 GitHub Actions 至少运行：

- 格式/静态检查（Ruff，mypy 范围逐步扩大）；
- Django system check 和迁移一致性检查；
- PostgreSQL 上的 pytest；
- Provider 契约测试（使用固定响应，不访问真实网络）；
- 幂等重跑测试；
- 权限隔离测试；
- 时区/DST 边界测试；
- 指数偏移、财报日期变化和通知去重的服务层测试。

真实 Provider 的小规模 smoke test 应与普通 PR CI 分离，避免速率限制和上游不稳定破坏常规测试。

## 11. 可观测性与运维

结构化日志至少包含 `run_id`、provider、任务类型、开始/结束时间、抓取/新增/更新/跳过/失败数量和错误分类，不记录密码、令牌、邮件正文或完整敏感响应。

健康检查分两类：

- Web 存活/数据库连通性，用于平台探针；
- 数据新鲜度、任务长期未运行、Provider 失败和通知积压，用于管理员检查。

MVP 可先以日志、Django Admin 和邮件告警运维，不新增独立监控产品功能。告警阈值、日志保留和错误追踪服务待部署决策。

## 12. MVP 与第二阶段边界

| 能力 | MVP | 第二阶段 |
|---|---|---|
| 财报、SEC、四指数、自选股、提醒 | 是 | 迭代 |
| Django Templates + HTMX | 是 | 继续使用，必要时评估增强 |
| 邮件、站内通知 | 是 | Telegram、Web Push 可加入 |
| PostgreSQL Cron 队列 | 是 | 负载达到阈值再评估 Celery/Redis |
| 市场热门 | 否 | 是 |
| Reddit 聚合 | 否 | 是 |
| PWA、自选股分组 | 否 | 是 |
| 对象存储 | 否，先受控存 DB | 原始数据增长后评估 |

升级到 Celery/Redis 不能仅按日期决定；应以任务明显超出 Cron 时限、并发需求、通知延迟或数据库队列争用的实测指标为依据。

## 13. 已识别的需求张力与已决澄清

1. SEC 和通知要求 5 分钟级任务，而生产基线只给出 Cron Job、且禁用常驻 Worker；必须验证托管平台频率和运行时限制，必要时用单一短生命周期 dispatcher，但不引入 Celery。
2. “每条关键数据保存每次抓取的原始响应”与“重复同步不得产生重复原始响应”存在表述张力；本方案对相同内容只存一份正文，同时每次运行保留获取/未变统计。
3. MVP 验收要求用户可以注册，待确认项又建议开发阶段关闭公开注册；路线图已拆为关闭注册的 alpha 和开放注册的 beta。
4. “所有数据库时间保存 UTC”不适用于只精确到自然日的公告日/生效日；本方案将其保存为 `date`，只有具体时刻使用 UTC。
5. PRD 使用公司级 IndexMembership、单一 `FILED` 状态和早期财报唯一键作为需求草案表达；ADR-001/002/003 已确定更精确的技术模型，产品范围未改变。

## 14. 待产品负责人确认

- 默认语言与 MVP 是否需要双语；
- MVP 正式环境是否开放注册；
- 邮件供应商、发件域名和失败/退信处理；
- 四个指数各自合法、稳定且允许再展示的数据来源；
- 财报日历供应商、字段语义、许可和更新频率；
- IR Provider 首批公司范围与维护方式；
- 来源冲突优先级、置信度规则和管理员复核流程；
- 候选财报事件跨 Provider 的自动合并阈值和取消后重排规则；
- 1–7 日指数偏移候选的人工复核负责人、时限与默认处理；
- release filing 首版允许使用的 exhibit/文本证据清单与复核时限；
- 日期提醒按美东自然日还是用户本地自然日计算；
- 摘要发送时间、每周摘要星期和 DST 行为；
- 原始数据保留期限、最大体积和删除政策；
- 通知最大重试次数及“永久失败”的定义；
- 开源协议（MIT 或 AGPL-3.0）；
- 生产托管平台是否最终确定为 Render，以及 Cron 限制验证结果。
