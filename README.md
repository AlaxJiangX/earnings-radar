# Earnings Radar

Earnings Radar 是面向个人使用的美股财报与指数变动雷达（当前单人使用，后续可能邀请朋友使用，不按大规模公开服务设计）。仓库已实现：Django + PostgreSQL + Docker Compose、基础认证与健康检查；DataSource/SyncRun 同步运行记录与 RawDataRecord/RawDataObservation/SourceEvidence/DataChange/AuditRecord 完整审计链；Company/SecurityListing 公司身份；MarketIndex/IndexMembership 生命周期、指数变化识别（含公开页面 `/index-changes`）与离线快照摄取；财报事件领域全生命周期（候选/正式身份、日期精度与变更历史、状态机、候选晋升、reconciliation 与财报页面）；SEC Filing 摄取与 Filing↔Earnings 链接分类；IR 确认与 Alpha Vantage 参考日历的 fixture-first 实现（真实来源分别受 allowlist 与许可 gate 阻塞）；Watchlist 与监控池 v2；仪表盘与自选股页面（5.2）；提醒规则与优先级计算（6.1，美东自然日锚定见 ADR-024）；通知入队、稳定幂等键与抑制记录（6.2，事务边界与 PD-4 见 ADR-025）；站内通知与设置页面（6.3）；邮件投递、每日摘要桶与失败重试（6.4，通用 SMTP 适配器 + fake provider 测试，契约见 ADR-027，cron 注册清单与生产部署步骤见 `docs/deployment.md`）；公开首页九区块组合页与数据来源信息页（7.1，首页含按当前用户隔离的自选股区块，来源页公开数据来源与新鲜度目标）；Django Admin 运维与手工修正（7.2：核心对象/来源冲突/同步运行/通知记录的查看筛选，权限保护并防重入的 SEC 手工同步，带原因与前后值审计的公司修正和 reconciliation 人工裁决，原始数据与审计记录默认只读）；分离式健康检查与端到端验收（7.3：轻量存活探针与管理员专用 `/health/data/` 数据健康页，SEC/财报日历/IR/指数公告/P0-P1 通知五路新鲜度按 `docs/data-sources.md` 统一起止点实测并诚实呈现 blocked/not_configured 等状态，任务过期、来源失败与通知积压可发现，关键用户旅程、四指数、SEC、指数变化与全套同步幂等的端到端测试）。阶段 8.1 部署准备已交付：生产运行配置（gunicorn 配置、collectstatic、托管 Postgres 连接串接入、生产安全默认）、Railway 部署手册与 Cron 清单、备份/恢复/恢复演练脚本与 ADR-028 保留策略契约（均见 `docs/deployment.md` 与 `docs/decisions/`）；实际部署与发布（8.2 起）尚未开始。阶段进度以 `docs/development-roadmap.md` 为准。

## 本地启动

```bash
cp .env.example .env
docker compose up --build
```

`.env.example` 中的 `DJANGO_SECRET_KEY` 和 `AUDIT_IP_HASH_KEY` 是两个不同用途的占位符。本地开发可替换为本地值；生产环境必须分别注入两个不同的强随机密钥，缺少独立审计哈希密钥时应用会拒绝启动。

启动后：

- 健康检查：<http://localhost:8000/health/>
- Django Admin：<http://localhost:8000/admin/>

首次启动会在 PostgreSQL 中自动执行已提交迁移。停止服务：

```bash
docker compose down
```

需要同时删除本地数据库卷时才使用 `docker compose down --volumes`。

## 验证

```bash
./scripts/verify.sh                # 标准质量门：单容器 fail-fast 执行全部六项检查
./scripts/verify.sh -k <pattern>   # 开发中只跑定向 pytest 子集
```

等价于依次运行 `docker compose run --rm web` 的 ruff check、ruff format --check、manage.py check、makemigrations --check、mypy 与 pytest（定义见 `AGENTS.md` 第 11 节）。

## 开发约束

开始任何任务前，按 `AGENTS.md` 第 2 节的分级阅读要求执行（高风险任务全量阅读，普通阶段任务按 ADR 索引定向阅读）。一次只完成路线图中的一个明确任务，不得提前实现第二阶段能力或未授权的核心业务模型。

审计数据的 URL userinfo 会被拒绝，敏感查询值会被稳定脱敏，URL fragment 不参与保存或请求指纹；明显认证凭据不得写入同步 scope、原始正文、SourceEvidence、DataChange 或 AuditRecord。安全查询条件仍会参与指纹，因此不同分页或业务筛选不会被错误合并。字段变化和操作审计只能通过受控 Service 追加，Admin 只显示截断预览且不允许增删改。

Provider 只返回带安全 URL、请求身份/指纹、HTTP 元数据、原始 bytes 和时区感知时间的结构化结果，不写数据库。HTTP client 必须显式注入 transport；真实传输层只有受限的 `BoundedHttpsTransport` 与 `SecHttpsTransport`（基于标准库，无新增 HTTP 依赖：单一批准 host、HTTPS-only、不跟随重定向、限量读取；Alpha Vantage 传输的凭据只在发送边界注入，SEC 传输无凭据且内置限速），仅用于已过许可 gate 的 SEC EDGAR 与 Alpha Vantage Provider。普通测试全部使用 Fake transport 与 fixture，不访问真实网络；任何新增真实来源仍必须先通过来源与许可确认，并由同步编排 Service 通过 `audit.services` 落库。

Company 使用规范化的 10 位 CIK（保留前导零）或预分配 UUID 作为稳定身份；ticker 仅属于 `SecurityListing` 的交易所与有效期记录。相同交易所/ticker 的有效期不能重叠，历史 ticker 通过关闭 `[start, end)` 半开区间并创建后继 listing 保留，不能原地覆盖 company、ticker、exchange 或有效期。公司和上市身份只能通过 `companies.services` 创建、更新或受控切换：创建追加 `AuditRecord`，字段变化追加带来源或运行记录的 `DataChange`；切换的重跑还必须核对关闭 DataChange（包括按创建时同一公共规则重算的 `change_key`）、旧 listing AuditRecord 和后继创建 AuditRecord 全部存在且一致；字段正确但 `change_key` 不一致同样会拒绝成功并要求人工核查，系统不会自动改写或补造历史。Service 使用 SourceEvidence 时会从数据库重新验证证据、任务、数据来源与原始观察链。Admin 默认只读：列表仅查看、搜索和筛选；仅有的写入口是权限保护、必须填原因并走上述 Service 审计链的专用动作（公司手工修正、reconciliation 人工裁决、SEC 手工同步触发），不能绕过这些入口修改数据。
