# Earnings Radar

Earnings Radar 当前处于数据基础阶段。仓库已经包含 Django、PostgreSQL、Docker Compose、基础认证用户、健康检查、DataSource/SyncRun 运行记录、RawDataRecord/RawDataObservation 原始响应去重与观察记录、SourceEvidence 来源证据，以及统一的凭据拦截、URL 清理和安全请求指纹；DataChange/AuditRecord 变更审计、财报、SEC、指数、Provider、通知等后续能力尚未实现。

## 本地启动

```bash
cp .env.example .env
docker compose up --build
```

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

审计数据的 URL userinfo 会被拒绝，敏感查询值会被稳定脱敏，URL fragment 不参与保存或请求指纹；明显认证凭据不得写入同步 scope、原始正文或 SourceEvidence。安全查询条件仍会参与指纹，因此不同分页或业务筛选不会被错误合并。
