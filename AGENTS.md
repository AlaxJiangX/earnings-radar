# Earnings Radar Codex 开发规范

本文件对仓库根目录及全部子目录持续生效。它规定 Codex 和其他开发代理如何读取需求、选择任务、修改项目和验证结果。

## 1. 每次任务开始前必须阅读

按以下顺序完整阅读：

1. `docs/product-requirements.md`：长期产品需求基线；
2. `docs/architecture.md`：模块边界、数据流和部署约束；
3. `docs/data-model.md`：实体、历史、来源和幂等约束；
4. `docs/data-sources.md`：外部来源边界、许可检查和数据新鲜度目标；
5. `docs/decisions/` 中所有 ADR：已接受决策与仍待确认的实现门；
6. `docs/development-roadmap.md`：阶段、前置条件和验收标准；
7. 当前目录或子目录中更具体的 `AGENTS.md`（若未来存在）。

不得只依赖聊天摘要或记忆代替仓库文件。开始工作前用 `git status`、`git branch --show-current` 和相关文件检查当前状态，保留用户已有改动。

若 PRD、架构、数据模型、ADR、数据源文档和路线图互相冲突：PRD 的产品范围优先，已接受 ADR 的技术建模决策优先于较早的技术草案；安全、隐私和数据完整性约束不得降低。不要自行猜测会改变产品行为的规则，应停止相关实现并列出需要产品负责人确认的决策。

## 2. 一次只完成一个明确任务

- 每次开发只选择 `docs/development-roadmap.md` 中一个编号小阶段，或用户明确指定的更小任务。
- 开始前写明：本次阶段、交付物、不会处理的相邻功能、验收命令。
- 满足本阶段验收标准后停止并汇报；不得自动进入下一阶段。
- 不得借“顺手重构”实现第二阶段或未经 PRD 说明的新功能。
- 若前置阶段未完成，只补齐本任务不可缺少的最小前置，并明确报告；不要扩大范围。
- 市场热门、Reddit、Telegram、Web Push、PWA、自选股分组、Celery、Redis、微服务和 SPA 均不属于 MVP，除非用户明确进入相应第二阶段。

当前仓库已完成阶段 1 工程骨架、阶段 2.1A 的 DataSource/SyncRun 基础、阶段 2.1B-1 的 RawDataRecord/RawDataObservation、阶段 2.1B-2 的 SourceEvidence、阶段 2.1B-3 的 DataChange/AuditRecord、阶段 2.2 的 Provider 契约、HTTP 传输接口与纯测试 Fake，以及阶段 2.3 的 Company/SecurityListing 身份基础。除非用户在后续任务中明确指定对应路线图小阶段，否则不要创建财报、指数、SEC、真实 Provider、自选股或通知业务实现。

## 3. 固定技术基线

- Python + Django；
- Django 模块化单体，不拆微服务；
- PostgreSQL；
- 本地 Docker Compose；
- 生产初期使用 Web Service、PostgreSQL、Cron Job 和第三方邮件服务；
- MVP 不使用 Celery 和 Redis；
- 页面优先 Django Templates，HTMX 用于渐进增强；
- 所有具体时刻以时区感知值写入 PostgreSQL，并统一为 UTC；纯自然日使用 `date`；
- 配置和密钥从环境变量读取，绝不提交 `.env` 或真实凭据；
- 审计 IP 哈希使用独立的 `AUDIT_IP_HASH_KEY`，不得复用或回退到 Django `SECRET_KEY`；生产环境缺失或使用开发默认值必须拒绝启动，轮换不得覆盖旧审计记录；
- 代码托管在 GitHub，数据库迁移必须随代码提交。

改变任何基线必须先写明理由、影响和迁移路径，并获得用户确认。

## 4. 模块边界

MVP 模块为 `accounts`、`companies`、`indexes`、`earnings`、`filings`、`watchlists`、`notifications`、`providers`、`audit`。`market_trends` 和 `social_signals` 仅在第二阶段创建。

- View 只处理 HTTP、权限、输入和渲染；不写核心业务规则，不直接访问外部源。
- 业务状态转换放在 service/use-case 层；复杂只读查询放 selector/query 层。
- Provider 只负责外部协议和安全的原始响应结果，不直接写领域业务表，也不创建 `SyncRun`、`RawDataRecord`、`RawDataObservation` 或通知。
- 未来同步编排 Service 负责创建 `SyncRun`、调用 Provider，并且只通过 `audit.services` 保存原始记录与观察关系；Provider 不得导入 audit 模型或写入 Service。
- 模块间通过公开 service/selector 和稳定模型引用协作，禁止循环依赖和跨 app 调用私有实现。
- Django Admin 是受权限控制的运维入口，不是绕过领域约束的后门。

详细依赖方向以 `docs/architecture.md` 为准。

## 5. 外部数据规则

所有外部数据源必须实现 Provider 适配器，并满足：

- 明确的连接/读取超时、限速、User-Agent 和有限重试；
- 区分临时错误、永久错误和数据校验错误；
- 未来同步编排器收到 ProviderResult 后，先通过 audit Service 保存原始响应，再标准化和核对；
- 保存 DataSource、来源 URL、抓取时间、内容哈希、原始值、标准化值、Provider/解析器版本和 SyncRun；
- 测试使用固定 fixture/fake，不在普通 CI 中访问真实网络；
- 真实 smoke test 单独运行并尊重上游使用条款；
- 不把供应商特有字段泄漏到领域模型，除非有明确映射和来源证据；
- 不静默吞掉来源冲突，不用低可信来源无审计覆盖高可信当前值。

指数和财报供应商、许可尚未确认前，只能设计契约或使用测试夹具，不得以临时抓取方案进入生产。

## 6. 幂等、并发与历史

每个同步、变更检测和通知任务必须能安全重跑：

- 使用稳定业务键和数据库唯一约束；
- 并发写入不能只依赖“先查后写”；
- 同类任务使用 PostgreSQL 锁防止重叠；
- 重复运行不得产生重复 Company、EarningsEvent、Filing、IndexChangeEvent、Notification 或原始正文；
- 值没有变化时不创建 DataChange、日期变化或通知；
- 当前值变化时，在可靠事务中保存旧值、新值、来源、任务 ID、操作者/系统身份和原因；
- DataChange 和 AuditRecord 只能通过 `audit.services` 的公开写入函数追加；不得用 Admin、模型实例更新、QuerySet update/delete 或通用写库接口改写/删除历史；
- Company 和 SecurityListing 只能通过 `companies.services` 的公开写入函数创建或更新；创建必须追加 AuditRecord，字段变化必须追加 DataChange，Admin 不得成为写入后门。SecurityListing 的 company、ticker、exchange 和有效期不得原地改写，必须通过原子后继切换关闭旧区间并创建新记录；幂等重放除领域记录外还必须精确验证该切换的 DataChange（包括用 audit 公共计算函数重算并核对 `change_key`）、旧 listing AuditRecord 和后继创建 AuditRecord，字段正确但 key 不一致同样属于异常历史，必须拒绝并人工核查，绝不自动补写或改写；
- 关键历史记录使用结束有效期、停用、取消或修正，不物理删除；
- 指数偏移保留底层加入/移除原子事实，再创建聚合事件；
- 通知使用稳定 `idempotency_key`，发送尝试单独追加记录。

每项相关功能至少包含“同一输入连续执行两次”的自动测试。

## 7. 数据与时间约束

- CIK 作为字符串保存并保留前导零；ticker 是可变上市身份，不能当永久公司主键。
- IndexMembership 必须关联 SecurityListing；Company 的指数归属和监控状态通过其 listing 聚合，不能把多 share class 压成一条公司级成员关系。
- SEC Filing 按 accession number 去重。
- EarningsEvent 只表达预计、确认、发布和取消生命周期；不得把 `FILED` 放入单向财报状态机。
- 年度财报内部统一为 `FY + includes_q4=true`；52/53 周使用 `fiscal_calendar_type` 和 `period_length_weeks`，不得扩张 period_type。
- SEC 文件可用性由 Filing 和 FilingEarningsLink 独立表达；release filing 使用 YES/NO/REVIEW_REQUIRED 并保存分类原因和规则版本。
- 指数变化的底层事实只有 IndexChangeLeg 的 `ADDED` / `REMOVED`；同日加入/移除自动合并，1–7 日进入待复核，超过 7 日保持独立。
- Russell 2000 与三个大型指数之间才使用 UPGRADE/DOWNGRADE；三个大型指数内部为 CROSS_INDEX。
- 监控影响必须区分 ENTERS_BASE_POOL 与 REENTERS_BASE_POOL，后者仅用于历史上退出后重新进入。
- 财报预计、确认、实际发布和电话会时间分字段保存；SEC 提交时间保存在 Filing 中。
- 页面必须明确显示时区；用户时区只影响显示和提醒窗口，不改变存储值。
- 时间相关测试覆盖 UTC、美东时间、用户本地时间和 DST 切换。
- 用户资源的每个 query 和 mutation 都必须按当前用户过滤；前端隐藏不等于权限控制。
- 审计、日志和原始响应不得包含密码、API key、session、认证头或其他秘密。
- 领域 Service 使用 SourceEvidence 时，只能以传入主键从数据库重新加载，并验证持久化 target、SyncRun、DataSource 和 RawDataObservation 链；不得信任调用方内存中修改过的来源字段。

## 8. 修改纪律

- 先检查现有实现和测试，再编辑；使用小而聚焦的补丁。
- 不修改 `docs/product-requirements.md`，除非用户明确要求更新 PRD；需要澄清时在规划/决策文档记录，不擅自重写需求。
- 模型变化必须包含迁移、唯一/检查约束和数据迁移影响说明。
- 不使用破坏性 Git 命令，不覆盖或删除不属于当前任务的用户改动。
- 不提交生成物、数据库文件、真实原始大响应、日志、缓存、虚拟环境或秘密。
- 不增加依赖，除非本阶段确实需要；说明用途、许可证/维护风险，并更新锁文件。
- 不把未实现能力写成 README 中的已完成能力。

## 9. 测试与验收

按改动风险选择测试，至少运行本阶段相关测试。完整实现阶段的默认验证目标包括：

- Ruff；
- mypy（按项目已启用范围）；
- Django system check；
- migration consistency check；
- PostgreSQL 上的 pytest；
- Provider 契约测试；
- 权限隔离、幂等重跑、来源追溯和时区边界测试。

不能运行测试时，不得声称通过；报告缺失工具、原因和未验证风险。修复失败只限本阶段范围，不借机重构无关代码。

## 10. 每次任务结束时的报告

报告必须包含：

1. 完成的路线图阶段/明确任务；
2. 创建或修改的文件；
3. 实际运行的验证命令及结果；
4. 数据迁移、部署或兼容性影响；
5. 发现的需求冲突和待产品确认决策；
6. 尚未完成或刻意排除的内容；
7. 推荐的下一个单一小阶段，但不要自动开始。

如果本次只是规划，明确说明没有编写业务代码、没有初始化 Django、没有安装依赖。
