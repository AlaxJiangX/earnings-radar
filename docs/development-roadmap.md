# Earnings Radar 开发路线图

> 状态：规划稿。阶段 1 工程骨架、阶段 2.1A 和 2.1B-1 至 2.1B-3 的来源、原始数据、证据、变更与操作审计基础已完成；核心领域模型和真实 Provider 仍未开始。
>
> 执行原则：一次开发任务只选择一个“小阶段”，满足该阶段验收标准后停止并汇报；不得顺手实现后续阶段。

## 1. 阶段规则

- MVP 为阶段 0–8；阶段 9 为第二阶段，不阻塞 MVP 发布。
- 每个小阶段开始前必须阅读 `docs/product-requirements.md`、`docs/architecture.md`、`docs/data-model.md`、`docs/data-sources.md`、全部 ADR、本路线图和根目录 `AGENTS.md`。
- 开始阶段前检查其前置条件；尚未确认且会改变实现的产品决策，必须先记录并请产品负责人确认。
- 每阶段提交应小而可审查，包含实现、迁移、测试和必要文档；不得把多个阶段捆成一次“大提交”。
- 所有 Provider、同步、变更检测和通知阶段都必须包含幂等重跑测试与来源追溯测试。
- 若实现发现 PRD 与规划冲突，停止扩展范围，先更新决策记录或请求确认。

## 2. MVP 路线

### 阶段 0：规划与需求基线（本次任务）

#### 0.1 固化需求与协作规范

交付：

- 将 PRD v0.1 放入 `docs/product-requirements.md`；
- 创建架构、数据模型、数据源规划、ADR 和开发路线图；
- 创建根目录 `AGENTS.md`；
- 标记冲突与待确认项。

验收标准：

- PRD 内容与提供的源文件一致，未被改写；
- 数据模型覆盖用户、公司、ticker/CIK、指数及关系、指数变化、财报及日期变更、SEC、自选股、提醒、通知、来源、原始数据和审计；
- 架构明确模块化单体、PostgreSQL、Docker Compose、Cron、Provider、UTC、幂等与历史追溯；
- MVP 和第二阶段有明确边界；
- 本阶段没有应用代码、Django 初始化或依赖安装。

#### 0.2 产品/数据源决策门（开发前）

交付：

- 产品负责人确认注册策略、提醒“提前一天”的时区语义；初始数据新鲜度和 alpha/beta 门槛已由 ADR-004 确定；
- 选定并记录财报日历和四个指数的数据来源、许可、字段与频率；
- 选定邮件供应商；
- 来源冲突矩阵仍待确认；指数偏移窗口和方向已由 ADR-002 确定；
- 落实已接受 ADR-001（财报身份）、ADR-002（证券级成员与偏移）、ADR-003（release filing 分类）和 ADR-004（发布门槛）的实现门。

验收标准：

- 每个外部源有用途、许可边界、认证方式、速率限制和失败策略；
- 未决项有负责人和最晚确认阶段；
- 不以临时代码替代尚未确认的业务规则。

### 阶段 1：工程基础

#### 1.1 仓库治理与最小项目骨架

交付：README、LICENSE（待确认后）、CONTRIBUTING、SECURITY、CHANGELOG、Issue/PR 模板，以及 Django 项目骨架。

验收标准：

- 仅创建配置与空模块，不实现业务页面；
- 自定义 User 在第一次迁移前确定；
- 配置从环境变量读取，`.env` 被忽略，`.env.example` 无秘密；
- Django system check 通过；
- README 区分尚未实现与已实现能力。

#### 1.2 PostgreSQL 与 Docker Compose

交付：本地 `web`、`db` 服务和可重复的启动流程。

验收标准：

- `docker compose up` 能启动 Web 和 PostgreSQL；
- 新数据库可执行全部迁移；
- 所有 DateTime 配置为时区感知并保存 UTC；
- Compose 中没有 Redis/Celery；
- 停止和重启容器不丢数据库卷数据。

#### 1.3 测试、质量与 GitHub Actions

交付：pytest、Ruff、mypy 基线和 CI。

验收标准：

- 本地与 CI 使用 PostgreSQL 跑测试；
- CI 执行 lint、类型检查约定、Django check、迁移检查和测试；
- PR 测试不访问真实外部 Provider；
- GitHub Actions 通过。

#### 1.4 用户认证与管理后台

交付：注册策略开关、登录/登出、自定义 User、时区偏好、Django Admin 权限基线。

验收标准：

- 注册行为符合 0.2 的确认结果；
- 密码使用 Django 哈希，CSRF 生效；
- 普通用户不能访问 Admin；
- 时区仅影响展示，不改变数据库 UTC；
- 权限与时区有自动测试。

### 阶段 2：来源与公司主数据

#### 2.1A 数据来源与同步运行基础

交付：DataSource、SyncRun、受控状态转换 service 和默认只读的 SyncRun Admin。

验收标准：

- 同一来源、任务和计划窗口的幂等键不重复，service 重试返回已有运行；
- 每次运行保存状态、开始/结束/心跳时间、版本和五类计数；
- 计数非负、终态必须有结束时间、结束时间不早于开始时间；
- 失败摘要经受控 service 压缩和常见凭据脱敏后保存；
- DataSource.base_url 通过模型和 Admin 校验拒绝 userinfo、真实敏感查询值与明显认证凭据；
- SyncRun scope 使用统一递归安全检查，凭据字段不写入审计 JSON；
- SyncRun 不能通过 Admin 新增、修改状态或删除；
- 不访问真实网络，不创建原始数据或核心领域模型。

#### 2.1B-1 原始数据正文与观察

交付：RawDataRecord、RawDataObservation、受控写入/解析状态 service 和只读 Admin。

验收标准：

- 相同来源、请求指纹和正文重复获取不会复制 payload；
- 同一运行幂等重跑不复制 observation，后续运行观察相同正文会追加自己的 observation；
- 内容 SHA-256、脱敏请求指纹、payload 实际大小和解析状态受 service 与数据库约束保护；
- source URL 拒绝 userinfo、移除 fragment，并以稳定标记替换敏感查询值；安全查询条件参与规范化指纹；
- 明显包含认证字段或 Authorization/Basic/Bearer 凭据的原始正文在落库前拒绝；
- 初始单条 payload 数据库硬上限为 1 MiB，运行配置只能下调；
- RawDataRecord 和 RawDataObservation 在 Admin 中不可新增、修改或删除；
- 测试不访问真实网络，不创建来源证据、变更审计或核心领域模型。

#### 2.1B-2 SourceEvidence 来源证据

交付：SourceEvidence 模型、受控幂等写入 service 和只读 Admin。

验收标准：

- 来源证据能追溯 RawDataRecord、RawDataObservation、SyncRun、DataSource、原始值、标准化值和规则版本；
- target 使用受限枚举和 UUID，不依赖未来领域 app，也不使用 GenericForeignKey；
- 数据库复合外键保证 sync_run/raw record 对应已存在 observation，service 额外校验来源一致；
- confidence、normalizer version、evidence_key 和 target type 有数据库约束；
- raw/normalized JSON 中的凭据键和显式认证文本被拒绝；
- URL、请求身份、同步 scope、来源证据和错误摘要共用集中安全规则，不在不同 Service 维护互相漂移的名单；
- 同一 RawDataRecord、目标、字段、标准化值和规则版本连续写入两次只保留一条证据；不同 RawDataRecord 的相同标准化事实分别留证；
- SourceEvidence Admin 可按权限查看，但不可新增、修改或删除；
- 不访问真实网络，不创建 DataChange、AuditRecord 或核心领域模型。

#### 2.1B-3 DataChange 与 AuditRecord

交付：DataChange、AuditRecord 的模型、受控写入 service 和只读/严格受控 Admin。

验收标准：

- 关键变化保存旧值、新值、来源证据、任务/操作者和原因；
- 管理修正要求原因，普通管理员不能编辑历史审计；
- 日志和审计不保存密钥；
- 同一未变化输入重跑不产生重复 DataChange 或 AuditRecord。
- target 和 action 使用受限枚举 + UUID，不依赖未来业务 app，不使用 GenericForeignKey；
- DataChange 规范化值相同直接跳过；变化使用包含 old/new、稳定来源和规则版本的唯一 SHA-256 change_key；
- 自动变化至少关联 SourceEvidence 或 SyncRun，人工修正关联 User、reason 和稳定 origin_key；可实现的不变量同时落到 PostgreSQL 检查/唯一约束；
- AuditRecord 的人工与系统入口分开，至少有 User 或 SyncRun，request_id 非空；原始 IP 只使用生产必填、与 Django SECRET_KEY 分离的 `AUDIT_IP_HASH_KEY` 转换为带版本的 keyed HMAC；
- old/new/before/after 复用集中递归安全检查，拒绝密码、Token、Session、Cookie、认证头、API key 和 URL 凭据；
- DataChange/AuditRecord 模型实例和 Admin 均不可改写或删除，Admin 不显示完整 JSON；
- 测试覆盖正常、失败、数据库约束、来源、权限、幂等、UTC 与敏感信息路径，且不访问真实网络。

#### 2.2 Provider 契约和测试夹具

交付：Provider 接口、错误分类、HTTP 超时/重试基类和固定响应夹具；不接真实业务源。

验收标准：

- View 不能调用 Provider；
- Provider 不直接写领域表；
- 超时、限速、临时失败、永久失败均有测试；
- 每次响应先形成可追溯 RawDataRecord。

#### 2.3 公司、CIK 与股票代码

交付：Company、SecurityListing、搜索 selector 和 Admin。

验收标准：

- 非空 CIK 唯一且保留前导零；
- 同 ticker 的历史/交易所身份可表达；
- 重复导入不产生重复公司或代码；
- 无 CIK 记录不能被错误确定性关联 SEC；
- NVDA 可由 ticker 和英文名称搜索（使用测试数据）。

### 阶段 3：指数能力

#### 3.1 指数与成分历史

交付：四个 MarketIndex、绑定 SecurityListing 的 IndexMembership 及有效期逻辑。

验收标准：

- 四指数可启用/停用；
- 同公司可通过不同 listing/share class 同时属于多个指数；
- 同一 SecurityListing 与指数的成分区间不重叠，历史结束不删除；
- Company 指数归属通过 listing 聚合，多个 share class 的底层事实不丢失；
- 单个 share class 移除后，监控池计算会检查其他 listing 和自选股；
- 给定日期可重建指数成分；
- 重复快照不产生重复关系。

#### 3.2 首个指数 Provider 与同步命令

交付：按已确认来源实现一个指数的端到端 Provider，再扩展至其他三个。

验收标准：

- 外部数据先保存原始响应和来源；
- 任务有超时、运行锁、计数和失败状态；
- 连续执行两次，第二次新增/变更均为零；
- 上游空响应或异常缩减不会未经保护地全量移除成分；
- 四个来源分别通过契约测试和受控 smoke test。

#### 3.3 加入、移除和偏移识别

交付：IndexChangeEvent、证券级 IndexChangeLeg，以及独立的原子动作、偏移方向和监控影响维度。

验收标准：

- IndexChangeLeg 的原子动作仅为 ADDED/REMOVED，历史完整；
- 同一生效日期自动合并，1–7 日形成待复核候选，超过 7 日保持独立；
- Russell 2000 与 LARGE 之间使用 UPGRADE/DOWNGRADE，三个 LARGE 指数内部使用 CROSS_INDEX，其他为 NONE；
- 监控影响独立使用 CONTINUES/ENTERS_BASE_POOL/EXITS_BASE_POOL/REENTERS_BASE_POOL，首次进入与历史重入正确区分；
- 同一事件可以同时表达偏移方向和监控影响，不互相覆盖；
- 修正/取消保留旧状态和来源；
- 重跑不重复创建事件。

#### 3.4 指数调整公开页面

交付：`/index-changes` 的列表、标签、筛选和 HTMX 分页。

验收标准：

- 可查看待生效、加入、移除、偏移和历史；
- 公告日期与生效日期分开显示；
- 访客可访问，登录用户看到自选标记但不能看到他人数据；
- 无 JavaScript 时核心筛选仍可用；
- 排序符合 PRD。

### 阶段 4：财报与 SEC

#### 4.1 财报事件领域模型

交付：EarningsEvent、EarningsDateChange、候选/正式身份、财报发布状态机和 Admin。开始编码前必须再次核对 ADR-001 与 ADR-003。

验收标准：

- 正式身份使用 company + period_end_date + period_type，并保存 identity_key/rule_version；
- period_end_date 未知时只创建可追溯、幂等的候选事件；
- 上游年度/Q4 标签统一为 FY 且 includes_q4=true，不产生 Q4/FY 双记录；
- 52/53 周使用 fiscal_calendar_type 和 period_length_weeks，不新增 period_type；
- 唯一规则通过 Q1–Q3、FY、H1/H2、外国发行人和 52/53 周财年测试；
- 状态只包含 SCHEDULED_ESTIMATED/SCHEDULED_CONFIRMED/RELEASED/CANCELLED，不包含 FILED；
- 预计、确认、发布和电话会时间独立；SEC 时间保存在 Filing；
- 非法状态倒退被阻止或必须走有审计的修正；
- 日期值不变时不生成变化；变化时保留旧值、新值和来源。

#### 4.2 财报日历 Provider 与每日同步

交付：选定供应商适配器和同步命令。

验收标准：

- 仅同步当前监控池和允许范围；
- 预计日期明确显示为预计，不冒充确认；
- 重跑不重复事件、变化或原始正文；
- 来源冲突按已批准规则处理并可追溯；
- 模糊/不完整数据进入待复核状态而非静默覆盖。

#### 4.3 财报列表与公司详情基础页

交付：`/earnings`、`/companies`、`/companies/{ticker}` 的 MVP 内容。

验收标准：

- 今日/本周/未来 30 天、状态、时段、指数可筛选；
- 公司页显示下一财报、状态、历史、来源和指数归属；
- 页面明确展示美东与用户本地时间（访客默认时区待确认）；
- DST 边界有测试；
- 列表查询无明显 N+1。

#### 4.4 SEC Provider 与 Filing

交付：SecEdgarProvider、CIK 匹配、Filing/FilingDocument、增量同步。

验收标准：

- accession number 唯一；
- 只处理目标表单和监控池；
- 遵守 SEC 访问标识、速率和重试政策；
- 不复制 SEC 全量正文，只保存要求的元数据、链接和证据；
- 5–15 分钟命令重跑无重复记录。

#### 4.5 财报与 Filing 关联、IR 有限确认

交付：FilingEarningsLink、规则版本/置信度、首批 IR Provider。

验收标准：

- 8-K 和定期报告可按规则关联财报事件；
- 不确定关联可复核且不会静默覆盖；
- IR 官方确认可将 SCHEDULED_ESTIMATED 推进至 SCHEDULED_CONFIRMED；
- release filing 与 periodic filing 由 FilingEarningsLink 独立表达，不改变 EarningsEvent.status；
- release filing 使用 YES/NO/REVIEW_REQUIRED，并保存分类原因和规则版本；
- 只有 YES 推导 has_release_filing，REVIEW_REQUIRED 不触发“已提交”通知；
- 页面查询可同时得到“财报已发布、8-K 已提交、10-Q 待提交”；
- 首批 IR 公司范围有清单；
- 所有字段可回溯到原始来源。

### 阶段 5：自选股与个人页面

#### 5.1 Watchlist 与监控池

交付：WatchlistItem、普通/重点、自选开关及监控状态重算。

验收标准：

- 用户可添加、移除并改变优先级；
- 用户只能操作自己的条目；
- 同一公司重复添加不重复；
- 公司在任一指数或任一有效自选股中即继续监控；
- 全部退出后停止未来普通同步但历史不删除。

#### 5.2 仪表盘和自选股页面

交付：`/dashboard` 和 `/watchlist`。

验收标准：

- 重点/普通自选股、未来 7 天事件和最近变化正确显示；
- 支持按下一财报和提醒优先级排序；
- HTMX 操作有 CSRF、权限和非 JS 回退；
- 跨用户数据隔离测试通过。

### 阶段 6：提醒与通知

#### 6.1 提醒规则与优先级

交付：UserNotificationPreference、ReminderRule、S/A/B/C 与 P0–P3 计算服务。

验收标准：

- PRD 中每种 MVP 提醒有可测试规则；
- 公司规则与用户默认覆盖方式符合确认结果；
- 单家公司总开关有效；
- 同一输入重复计算结果稳定；
- 不实现市场/Reddit 热度权重。

#### 6.2 Notification 入队与去重

交付：Notification、稳定幂等键和领域变化到通知的映射。

验收标准：

- 日期变化、发布、SEC、指数加入/移除/偏移和提前一天可入队；
- 同一事件重跑不会创建第二条同渠道通知；
- 当前状态、变更记录和通知入队处于可靠事务边界；
- 用户关闭渠道或公司提醒时通知被抑制并记录原因。

#### 6.3 站内通知与历史页面

交付：站内通知、已读状态、`/notifications`、`/settings/notifications`。

验收标准：

- 页面显示时间、公司、类型、优先级、状态和来源；
- 用户只能查看自己的通知；
- 已读与发送状态分离；
- 设置变更有 CSRF 和权限测试。

#### 6.4 邮件投递、摘要和失败重试

交付：邮件适配器、NotificationDeliveryAttempt、每 5 分钟发送命令。

验收标准：

- 邮件包含 PRD 指定字段与来源链接；
- 临时失败按有限策略重试，永久失败停止并记录；
- 两个并发发送命令不会领取同一通知；
- 摘要时间桶重跑不重复；
- 邮件服务凭据只在环境变量中；
- 测试使用 fake provider，不发送真实邮件。

### 阶段 7：整合、管理与健康检查

#### 7.1 首页与公开来源页面

验收标准：

- 首页包含 PRD 的 MVP 区块，不出现第二阶段热门区块；
- 未登录用户能查看公开财报、公司、SEC、指数变化和来源；
- GitHub 入口有效；
- 页面状态和空数据文案不误导用户。

#### 7.2 Admin 运维与手工修正

验收标准：

- 管理员能查看/筛选核心对象、来源冲突、同步运行和通知记录；
- 手工同步受权限保护并防重入；
- 关键修正要求原因、保留前后值和操作者；
- 原始数据和审计记录默认不可编辑。

#### 7.3 健康检查与端到端验收

验收标准：

- Web 存活与数据新鲜度检查分离；
- 任务过期、来源失败和通知积压可发现；
- SEC、财报日历、IR、指数公告和 P0/P1 通知的新鲜度均可按 `docs/data-sources.md` 的统一起止点测量；
- 关键用户旅程、四指数、日期变化、SEC 和通知端到端测试通过；
- 全套同步连续运行两次无重复关键数据和通知；
- 测试结果对应 PRD 第 17 节逐项记录。

### 阶段 8：v0.1 alpha、beta 与正式发布

#### 8.1 共同部署准备

验收标准：

- Web Service、PostgreSQL、Cron Job 和邮件服务配置完成；
- 未部署 Celery/Redis；
- 生产环境变量、HTTPS、迁移步骤和回滚步骤有文档；
- Cron 频率、并发、超时经平台实测满足 SEC/通知要求；
- 数据库备份已开启并完成一次恢复演练。

#### 8.2 v0.1.0-alpha.1：个人/内部测试

验收标准：

- README 能让新用户按 `git clone`、复制 `.env`、`docker compose up` 启动；
- GitHub CI 通过，许可证与安全政策已确认；
- 云端核心页面、Provider 新鲜度和测试邮件 smoke test 通过；
- 公开注册关闭，只允许管理员创建或邀请的个人/内部测试账号；
- 单条关键提醒可验证，完整每日/每周摘要不作为 alpha 开放条件；
- 发布 `v0.1.0-alpha.1`，明确测试数据范围、已知限制和反馈渠道；
- 没有把第二阶段能力标为已实现。

#### 8.3 alpha 退出评审

验收标准：

- 连续 14 个自然日没有 P0 事故、重复关键记录或重复 P0/P1 通知；
- 排除已确认上游中断后，同步成功率不低于 95%；
- SEC 与 P0/P1 通知至少 95% 可测样本达标，财报日历、IR 与指数公告至少 90% 可测样本达标；
- 无真实样本的类别使用 fixture 验证并明确披露，不能按 100% 达标；
- 指数多 share class、财报候选身份和 Filing 独立状态经过真实样本复核；
- 数据源失败、通知失败和管理员修正流程经过演练；
- alpha 发现的阻塞缺陷关闭，剩余限制进入 beta 发布说明。

#### 8.4 v0.1.0-beta.1：开放注册与完整摘要

验收标准：

- 开放注册策略、安全防护、邮件验证和滥用控制符合已确认方案；
- 每日摘要和每周摘要按用户时区、稳定时间桶和幂等键生成；
- 完整摘要在重跑、并发和失败重试时不重复发送；
- 用户权限隔离、退订/渠道开关和通知历史端到端测试通过；
- 连续 30 个自然日无 P0 事故和任何重复关键记录/通知/摘要；
- 排除已确认上游中断后，同步成功率不低于 99%；
- 五类新鲜度至少 95% 可测样本达到 ADR-004 初始目标；
- 邮件与站内摘要成功率不低于 99%；
- 发布 `v0.1.0-beta.1` 并记录已知限制。

#### 8.5 v0.1.0 正式版评审

验收标准：

- PRD 第 17 节 MVP 验收项全部通过并有证据；
- alpha/beta 未关闭的阻塞问题为零；
- 数据备份恢复、回滚、健康检查和运维手册完成；
- 发布 `v0.1.0`，仍不包含任何第二阶段能力。

## 3. 第二阶段路线（不属于 MVP）

只有 MVP 稳定性指标和产品反馈达到要求后才排期。

### 阶段 9.1：任务基础设施评估

根据 Cron 运行时、任务积压、通知延迟和数据库锁争用数据，决定是否引入 Celery、Redis、Background Worker 或对象存储。

验收标准：有量化瓶颈、迁移方案、回滚方案；没有指标则保持 MVP 架构。

### 阶段 9.2：市场热门

仅统计当前监控池，实现成交活跃、涨跌、相对成交量和财报临近异常，并解释每次上榜原因。

验收标准：数据来源和许可确认；计算可重放；榜单不输出投资建议或不透明综合分数。

### 阶段 9.3：Reddit 热门

只保存 PRD 允许的帖子元数据和聚合统计，不抓全量评论、不做复杂情绪判断。

验收标准：API/许可/保留政策确认；ticker 误识别可复核；每条榜单解释原因；删除和限速规则可执行。

### 阶段 9.4：其他体验扩展

Telegram、Web Push、PWA、自选股分组分别作为独立小阶段评审，不与市场/Reddit 模块捆绑。

## 4. 产品确认清单与最晚阶段

| 决策 | 最晚确认阶段 |
|---|---|
| 默认语言、alpha 账号策略、beta 公开注册、开源协议 | 1.1/1.4 前；公开注册最晚 8.4 前 |
| 财报/指数来源与许可 | 2.2 前，真实 Provider 开发前必须完成 |
| 邮件服务、摘要时间、重试规则 | 6.4 前 |
| 候选财报事件跨 Provider 合并阈值与取消重排 | 4.1 前；FY/52-53 周规则已由 ADR-001 确定 |
| 1–7 日指数候选复核负责人和时限 | 3.3 前；窗口、方向和 ENTERS/REENTERS 已由 ADR-002 确定 |
| release filing 证据清单、复核展示和时限 | 4.5 前；三态分类已由 ADR-003 确定 |
| 来源冲突与人工锁定策略 | 2.3 前 |
| 提前一天的时区/DST 语义 | 6.1 前 |
| 原始/通知/审计数据保留 | 8.1 前 |
| 新鲜度与 alpha/beta 门槛的后续调整 | 仅在 alpha 实测支持时修订 ADR-004 |
| 生产平台及 Cron 能力 | 8.1 前 |

## 5. 完成定义（适用于每个小阶段）

一个阶段只有同时满足以下条件才算完成：

- 范围与该阶段交付一致，没有夹带后续功能；
- 数据库变更有迁移、约束和回滚影响说明；
- 正常、权限、失败、幂等和必要时区边界测试通过；
- 新外部数据有 Provider、超时、来源、原始数据和契约测试；
- 关键数据变更有旧值、新值、来源、任务/操作者；
- 文档准确描述已实现状态和未决项；
- CI 通过；
- Codex 在阶段结束时报告改动、验证、风险与下一阶段，但不自动开始下一阶段。
