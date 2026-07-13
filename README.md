# Earnings Radar

Earnings Radar 当前处于工程骨架阶段。仓库已经包含 Django、PostgreSQL、Docker Compose、基础认证用户、健康检查和质量工具配置；财报、SEC、指数、Provider、通知等核心业务尚未实现。

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
