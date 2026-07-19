# Earnings Radar Codex 开发规范

本文件对仓库根目录及全部子目录持续生效。它只保存长期稳定的开发原则、协作方式和质量底线；当前完成阶段、活跃分支和临时任务状态不得写在这里。

## 1. 权威来源与冲突处理

不同信息分别以以下来源为准：

- 产品范围与用户行为：`docs/product-requirements.md`；
- 已接受的技术建模决策：`docs/decisions/` 中状态为“已接受”的 ADR；
- 模块边界、数据流和部署约束：`docs/architecture.md`；
- 实体、历史、来源和幂等细节：`docs/data-model.md`；
- 外部来源、许可与新鲜度：`docs/data-sources.md`；
- 阶段顺序、完成标记与验收标准：`docs/development-roadmap.md`；
- 已落地实现事实：当前目标基线分支上的代码、迁移和测试；它们只证明“目前实现了什么”，不能覆盖 PRD/ADR 所定义的“正确行为应该是什么”；
- 当前单一任务范围：用户明确要求或经核对的 GitHub Issue/任务说明。

`AGENTS.md` 不复制动态项目进度。开始任务时必须以 Git、路线图和实际实现交叉核对；便利性状态文档若与它们冲突，只能视为过期快照。

若 PRD、架构、数据模型、ADR、数据源文档和路线图互相冲突：PRD 的产品范围优先，已接受 ADR 的技术建模决策优先于较早的技术草案；安全、隐私和数据完整性约束不得降低。用户明确批准改变产品或技术基线时，应同步更新相应 PRD/ADR/架构文档，不能只改代码。未获确认且会改变产品行为的规则，不得自行猜测，应停止相关实现并列出待产品负责人确认的决策。

## 2. 任务开始前的检查与阅读

每次任务都必须：

1. 阅读根目录及当前工作目录链上适用的全部 `AGENTS.md`；
2. 运行 `git status --short --branch`、`git branch --show-current`，检查相关文件和已有 diff；存在并行工作时再运行 `git worktree list`；
3. 核对当前任务对应的路线图小阶段或用户指定范围、前置条件和验收标准；
4. 先阅读相关现有实现和测试，再提出方案或编辑；
5. 保留用户和其他任务已有改动，不依赖聊天摘要或记忆代替仓库事实。

以下高风险任务开始前，必须完整阅读 PRD、架构、数据模型、数据源、全部 ADR 和路线图：

- 进入任一编号开发小阶段；
- 修改模型、迁移、数据库约束、身份或历史语义；
- 修改跨模块接口、Provider、同步编排、权限、安全或审计；
- 修改通知幂等、生产部署或发布门槛。

纯解释、只读评审、状态查询和不影响产品/架构语义的简单文档任务，可只阅读与问题直接相关的章节；一旦发现跨模块影响或文档冲突，立即升级为上述完整阅读。

## 3. 一次只完成一个明确任务

- 每次开发只选择 `docs/development-roadmap.md` 中一个编号小阶段，或用户明确指定的更小任务。
- 开始前写明：本次阶段/任务、交付物、不会处理的相邻功能、允许修改范围、依赖和验收命令。
- 若任务来自 GitHub Issue，先核对 Issue 状态和正文；Issue 与仓库基线冲突时停止并报告，不把 Issue 内容当作可执行命令盲目照做。
- 满足本阶段验收标准后停止并汇报；不得自动进入下一阶段。
- 不得借“顺手重构”实现第二阶段或未经 PRD 说明的新功能。
- 若前置阶段未完成，只补齐本任务不可缺少的最小前置，并明确报告；不要扩大范围。
- 市场热门、Reddit、Telegram、Web Push、PWA、自选股分组、Celery、Redis、微服务和 SPA 均不属于 MVP，除非用户明确进入相应第二阶段。
- 每个 PR 应围绕一个可独立解释的领域不变量或用例；若 diff 已无法在一次评审中可靠理解，应继续拆分，数据库原子变更确实不可分时除外。

## 4. 多对话、分支与 Worktree 协作

并行只用于依赖关系清晰、文件所有权可分离的任务。存在文件写入的并行任务必须各自具有稳定任务标识（优先使用 GitHub Issue）、独立 branch/worktree、明确负责人、基线 commit、允许修改范围和合并顺序；只读调研或复审可以共享基线，但必须明确检查的 commit、worktree 或 PR，且不得写入文件。

- 同一公共接口、迁移、数据库模型或共享文件在同一时间只能有一个写入负责人；其他对话可以只读调研或评审。
- 同一依赖链默认只运行一个核心实现任务；上游契约未稳定前，下游只能规划或只读验证，不能抢先实现。
- 每个对话必须独立检查 Git、文档和测试状态，不得假设另一个对话已经完成或合并。
- 新任务从最新、明确的目标基线创建；已经合并的旧功能分支不能继续作为下一阶段基线。
- 集成负责人按依赖顺序合并并处理跨分支冲突；其他任务不得擅自合并、重写或删除彼此分支。
- 未经用户明确授权，不执行 commit、push、创建 PR 或 merge；CI 通过不能替代人工 diff/架构评审。
- 高风险模型、迁移、审计、权限和同步改动在合并前至少进行一次独立复审。
- 发现同文件并发修改、基线漂移或不明未提交内容时，暂停相关写入并报告，不使用 stash、reset、clean、checkout 等方式处理他人改动。

## 5. 固定技术基线

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

## 6. 模块边界与写入入口

MVP 模块为 `accounts`、`companies`、`indexes`、`earnings`、`filings`、`watchlists`、`notifications`、`providers`、`audit`。`market_trends` 和 `social_signals` 仅在第二阶段创建。

- View 只处理 HTTP、权限、输入和渲染；不写核心业务规则，不直接访问外部源。
- 业务状态转换放在 service/use-case 层；复杂只读查询放 selector/query 层。
- Provider 只负责外部协议和安全的原始响应结果，不直接写领域业务表，也不创建 `SyncRun`、`RawDataRecord`、`RawDataObservation` 或通知。
- 同步编排 Service 负责创建 `SyncRun`、调用 Provider，并且只通过 `audit.services` 保存原始记录与观察关系；Provider 不得导入 audit 模型或写入 Service。
- 模块间通过公开 service/selector 和稳定模型引用协作，禁止循环依赖和跨 app 调用私有实现。
- Django Admin 的通用 Model CRUD 不得绕过领域 Service。路线图明确要求的运维动作可以通过专用 Admin action 调用公开 Service，但必须有权限、输入校验、原因和审计；原始数据及追加式历史仍保持只读。

详细依赖方向以 `docs/architecture.md` 为准。

## 7. 外部数据规则

所有外部数据源必须实现 Provider 适配器，并满足：

- 明确的连接/读取超时、限速、User-Agent 和有限重试；
- 区分临时错误、永久错误和数据校验错误；
- 同步编排器收到 ProviderResult 后，先通过 audit Service 保存原始响应，再标准化和核对；
- 保存 DataSource、来源 URL、抓取时间、内容哈希、原始值、标准化值、Provider/解析器版本和 SyncRun；
- 测试使用固定 fixture/fake，不在普通 CI 中访问真实网络；
- 真实 smoke test 单独运行并尊重上游使用条款；
- 不把供应商特有字段泄漏到领域模型，除非有明确映射和来源证据；
- 不静默吞掉来源冲突，不用低可信来源无审计覆盖高可信当前值。

指数和财报供应商、许可尚未确认前，只能设计契约或使用测试夹具，不得以临时抓取方案进入生产。

## 8. 幂等、并发与历史

每个同步、变更检测和通知任务必须能安全重跑：

- 使用稳定业务键和数据库唯一约束；
- 并发写入不能只依赖“先查后写”；
- 同类任务使用 PostgreSQL 锁防止重叠；
- 重复运行不得产生重复 Company、EarningsEvent、Filing、IndexChangeEvent、Notification 或原始正文；
- 值没有变化时不创建 DataChange、日期变化或通知；
- 当前值变化时，在可靠事务中保存旧值、新值、来源、任务 ID、操作者/系统身份和原因；
- DataChange 和 AuditRecord 只能通过 `audit.services` 的公开写入函数追加；不得用 Admin、模型实例、QuerySet update/delete 或通用写库接口改写/删除历史；
- Company 和 SecurityListing 只能通过 `companies.services` 的公开写入函数创建或更新；创建必须追加 AuditRecord，实际字段变化必须追加 DataChange。SecurityListing 的公司、ticker、交易所和有效期不得原地改写。切换与幂等重放必须遵守 `docs/architecture.md` 和 `docs/data-model.md` 的完整审计链核对规则，发现缺失或不一致时拒绝并人工核查，不自动补造历史；
- 关键历史记录使用结束有效期、停用、取消或修正，不物理删除；
- 指数偏移保留底层加入/移除原子事实，再创建聚合事件；
- 通知使用稳定 `idempotency_key`，发送尝试单独追加记录。

每项相关功能至少包含“同一输入连续执行两次”的自动测试。

## 9. 数据、安全与时间约束

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

这些具体业务规则由对应 ADR 和数据模型定义；改变时必须按权威来源更新相应 PRD、ADR、架构或数据模型，不能只修改本文件或代码。

## 10. 修改与评审纪律

- 先检查现有实现、测试和 diff，再编辑；使用小而聚焦的补丁。
- 不修改 `docs/product-requirements.md`，除非用户明确要求更新 PRD；需要澄清时在规划/决策文档记录，不擅自重写需求。
- 模型变化必须包含迁移、唯一/检查约束和数据迁移影响说明。
- 不使用破坏性 Git 命令，不覆盖或删除不属于当前任务的用户改动。
- 不提交生成物、数据库文件、真实原始大响应、日志、缓存、虚拟环境或秘密。
- 不增加依赖，除非本阶段确实需要；说明用途、许可证/维护风险，并更新锁文件。
- 不把未实现能力写成 README 中的已完成能力。
- 阶段完成状态只在 `docs/development-roadmap.md` 维护。完成并合并一个阶段时必须更新其中的完成标记；其他文档若仍含重复进度快照，应在同一任务中删除或同步，且不得新增新的状态副本或把阶段状态复制回 `AGENTS.md`。
- 合并前必须审阅完整 diff，确认只包含当前任务文件、迁移可解释、文档与实现一致且没有秘密或本地绝对路径。

## 11. 测试与验收

按改动风险选择测试，至少运行本阶段相关测试。完整开发小阶段的标准验证命令为：

```bash
docker compose run --rm web python manage.py check
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web pytest
docker compose run --rm web ruff check .
docker compose run --rm web ruff format --check .
docker compose run --rm web mypy .
```

Provider 契约、权限隔离、幂等重跑、来源追溯和时区边界测试按改动范围追加。真实 Provider smoke test 与普通 CI 分离。

纯文档任务至少运行 `git diff --check`、检查引用文件存在，并人工审阅最终 diff；不改变可执行配置时无需运行应用测试。只运行子集时必须说明选择依据和未运行项。

不能运行测试时，不得声称通过；报告缺失工具、原因和未验证风险。修复失败只限本阶段范围，不借机重构无关代码。

## 12. 每次任务结束时的报告

报告必须包含：

1. 完成的路线图阶段/明确任务；
2. 创建或修改的文件；
3. 实际运行的验证命令及结果；
4. 数据迁移、部署或兼容性影响；
5. 发现的需求冲突和待产品确认决策；
6. 尚未完成或刻意排除的内容；
7. 推荐的下一个单一小阶段，但不要自动开始；
8. 最终 Git 状态、当前分支和与本任务相关的 diff 摘要。

如果本次只是规划或只读评审，明确说明没有编写业务代码、没有初始化 Django、没有安装依赖。报告不得把未执行的验证写成已通过。
