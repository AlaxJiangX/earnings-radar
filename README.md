# Earnings Radar

Earnings Radar 当前处于数据基础阶段。仓库已经包含 Django、PostgreSQL、Docker Compose、基础认证用户、健康检查、DataSource/SyncRun 运行记录、RawDataRecord/RawDataObservation 原始响应去重与观察记录、SourceEvidence 来源证据、DataChange 字段变更历史、AuditRecord 操作审计、Company/SecurityListing 公司身份基础，以及 Provider 契约、安全 HTTP 传输接口和完全离线的 Fake/fixture。财报、SEC、指数、真实 Provider、同步编排和通知等后续能力尚未实现。

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
docker compose run --rm web python manage.py check
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web pytest
docker compose run --rm web ruff check .
docker compose run --rm web ruff format --check .
docker compose run --rm web mypy .
```

## 开发约束

开始任何任务前，完整阅读 `AGENTS.md`、PRD、架构、数据模型、数据源、全部 ADR 和开发路线图。一次只完成路线图中的一个明确任务，不得提前实现第二阶段能力或未授权的核心业务模型。

审计数据的 URL userinfo 会被拒绝，敏感查询值会被稳定脱敏，URL fragment 不参与保存或请求指纹；明显认证凭据不得写入同步 scope、原始正文、SourceEvidence、DataChange 或 AuditRecord。安全查询条件仍会参与指纹，因此不同分页或业务筛选不会被错误合并。字段变化和操作审计只能通过受控 Service 追加，Admin 只显示截断预览且不允许增删改。

Provider 只返回带安全 URL、请求身份/指纹、HTTP 元数据、原始 bytes 和时区感知时间的结构化结果，不写数据库。当前 HTTP client 必须显式注入 transport，仓库只提供 FakeTransport，不存在默认真实网络实现，也没有新增 HTTP 依赖。普通测试会阻断真实 HTTP；任何真实 Provider 都必须等待来源和许可确认，并由未来同步编排 Service 通过 `audit.services` 落库。

Company 使用规范化的 10 位 CIK（保留前导零）或预分配 UUID 作为稳定身份；ticker 仅属于 `SecurityListing` 的交易所与有效期记录。相同交易所/ticker 的有效期不能重叠，历史 ticker 通过关闭 `[start, end)` 半开区间并创建后继 listing 保留，不能原地覆盖 company、ticker、exchange 或有效期。公司和上市身份只能通过 `companies.services` 创建、更新或受控切换：创建追加 `AuditRecord`，字段变化追加带来源或运行记录的 `DataChange`；切换的重跑还必须核对关闭 DataChange（包括按创建时同一公共规则重算的 `change_key`）、旧 listing AuditRecord 和后继创建 AuditRecord 全部存在且一致；字段正确但 `change_key` 不一致同样会拒绝成功并要求人工核查，系统不会自动改写或补造历史。Service 使用 SourceEvidence 时会从数据库重新验证证据、任务、数据来源与原始观察链。Admin 仅可查看、搜索和筛选，不能绕过该入口修改数据。
