# Earnings Radar 部署手册（Stage 8.1 交付）

> 状态：Stage 8.1「共同部署准备」交付物。本文让产品负责人能照着步骤独立完成
> 8.2 的实际部署；平台实测（Cron 真实触发、备份还原、云端 smoke）按 §9 的
> 清单在 8.2 执行。技术基线不变：无 Celery/Redis，一个 Web 容器 + 一个托管
> PostgreSQL + 短生命周期 Cron Job + 第三方邮件服务。

## 1. 平台选型结论（工程评估，2026-10-25）

产品负责人已拍板部署到 PaaS（Railway/Render 类）。工程对比官方文档后选 **Railway**：

| 对比项 | Railway | Render | 结论 |
|---|---|---|---|
| 定时任务 | 原生 Cron：5 字段 crontab（UTC），最小间隔 5 分钟；上一轮未结束则**跳过**本轮；官方注明执行可能有几分钟漂移 | 原生 Cron Jobs：任意 cron 频率，活跃运行时新运行**延迟**（不重叠保证）；单次运行最长 12 小时；每服务 $1/月起 | 两家都满足本项目 5 分钟级需求；Railway 漂移是 P0/P1 通知「5 分钟内」目标的已知压力点（见 §10） |
| 托管 PostgreSQL | 默认私有网络；`DATABASE_URL` 变量自动生成，其他服务可 `${{Postgres.DATABASE_URL}}` 引用；可选 HA 集群 | 内网连接串 + PITR；新实例支持 PG 13–18 | 等价 |
| 备份 | 卷级 Backups：**每日（留 6 天）+ 每周（留 27 天）+ 每月（留 89 天）**可叠加，一键 Restore | PITR 窗口 Hobby 3 天/Pro 7 天；导出留 7 天；30 天保留需自建 S3 上传管线 | **决定性差异**：Railway 零配置满足 ADR-028「备份每日、保留 30 天」，Render 需要额外对象存储账号与脚本 |
| Docker 部署 | 识别仓库 Dockerfile，支持每服务自定义启动命令 | Docker runtime，支持 Docker Command 覆写 | 等价 |
| 价格（alpha 规模） | Hobby $5/月起 + 用量（web + Postgres + 备份存储） | Web ~$7/月 + Postgres ~$7/月 + Cron 3×$1/月 | Railway 更低且按量 |

综上：Railway 在「满足已拍板的备份策略」「成本」「Postgres 默认私有」上更优；
代价（cron 最小 5 分钟、执行漂移）不影响 8.1 配置，但列入 §10 的 8.2 实测与
ADR-004 审计观察点。

## 2. 生产依赖（已随 8.2-A 进入 requirements.txt）

8.1 报告承诺的「第 0 步」已完成：产品负责人批准的两个生产依赖（精确 pin，
均为 MIT 许可）已进入 `requirements.txt`，部署时无需再手工添加：

- **gunicorn**（MIT 许可）：生产 WSGI 服务器，替换开发用 `runserver`；
  配置文件 `deploy/gunicorn.conf.py` 已随 8.1 就绪（`$PORT`、workers、超时
  全部环境变量可调）。
- **whitenoise**（MIT 许可）：让单个 Web 容器自己服务 `collectstatic` 产物
  （当前只有 Django Admin 的静态文件；站点模板没有自定义静态资源）。
  settings 已内置接线：`DJANGO_USE_WHITENOISE=true` 时启用；包未安装时
  **拒绝启动并报错**，无静默降级。

部署时在环境变量中设置 `DJANGO_USE_WHITENOISE=true`（§4 表格已含）。

## 3. 用户手工步骤（照做即可，均为只有账号所有者能做的事）

1. **Railway**：用 GitHub 账号登录 <https://railway.com>，New Project。
2. **数据库**：+ New → Database → PostgreSQL。进入该服务 → Backups 标签 →
   启用 **Daily、Weekly、Monthly** 三档（对应 ADR-028 备份策略，见 §7）。
3. **Web 服务**：+ New → GitHub Repo → 选本仓库。Railway 识别 `Dockerfile`。
   在 Settings → Deploy：
   - Custom Start Command:
     `python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi -c deploy/gunicorn.conf.py`
   - Networking → Generate Domain 得到 `https://<app>.up.railway.app`
     （可选：绑定自有域名，见 §8）。
4. **环境变量**：按 §4 清单填入 web 服务 Variables（数据库连接直接引用
   `${{Postgres.DATABASE_URL}}`）。
5. **Brevo 邮件**：注册 <https://app.brevo.com>（免费档 300 封/天）：
   1. Senders, Domains & Dedicated IPs → Senders → 添加并验证发件人
      （`DEFAULT_FROM_EMAIL` 必须用这个已验证发件人地址）；
   2. SMTP & API → 生成 **SMTP key**；
   3. `EMAIL_HOST_USER` = Brevo 登录邮箱，`EMAIL_HOST_PASSWORD` = 该 SMTP key，
      `EMAIL_HOST=smtp-relay.brevo.com`、`EMAIL_PORT=587`、`EMAIL_USE_TLS=true`。
6. **三个 Cron 服务**：每个都是 + New → GitHub Repo → 同一仓库，然后：
   - Settings → Deploy → Custom Start Command 填 §5 表中的命令；
   - Settings → Deploy → Cron Schedule 填 §5 表中的表达式（UTC）；
   - Variables 与 web 服务相同（含 `${{Postgres.DATABASE_URL}}`、全部
     `DJANGO_*`/`EMAIL_*`/`SEC_*`；可用 Railway shared variables 复制一份）。
7. **首次部署**：push 到 main（或 Railway 手动 Deploy）→ 观察 web 服务
   Deploy Logs：migrate 全部应用、collectstatic 完成、gunicorn 监听 8000。
8. **创建管理员**（二选一，以 Railway 当前 CLI 能力为准）：
   - CLI：本地 `npm i -g @railway/cli && railway login`，然后
     `railway ssh --service <web服务名> -- python manage.py createsuperuser`；
   - 备选：临时把 web 服务 Start Command 前置
     `python manage.py createsuperuser --noinput || true`（Variables 里临时加
     `DJANGO_SUPERUSER_EMAIL` / `DJANGO_SUPERUSER_PASSWORD`），部署成功后
     **立即**移除临时变量与命令前缀。
9. **创建 SEC DataSource 行**（cron 依赖它）：`/admin/` 登录 → audit →
   Data sources → Add：`key=sec_edgar`、`name=SEC EDGAR`、
   `source_type=SEC`、`base_url=https://data.sec.gov`、`is_official=✓`、
   `provider_adapter=sec-edgar`、`is_enabled=✓`。cron 命令里的
   `--source-key sec_edgar` 与此对应。
10. **验证**：浏览器访问 `https://<app>/health/`（期望
    `{"status":"ok","database":"ok"}`）与 `https://<app>/health/data/`
    （五条新鲜度路由；IR/指数应显示 blocked、财报日历若未配置显示
    not_configured，均属预期）；`/admin/` 能登录且样式正常（whitenoise 生效）。

## 4. 环境变量全清单

所有值只从环境变量读取，绝不提交 `.env` 或真实凭据。**必填**项缺失时应用
拒绝启动（沿用既有 fail-fast 行为）。

| 变量 | 必填 | 生产值（示例/规则） | 说明 |
|---|---|---|---|
| `DJANGO_ENV` | ✅ | `production` | 非本地环境强制走生产校验 |
| `DJANGO_SECRET_KEY` | ✅ | 长随机串（`python -c "import secrets; print(secrets.token_urlsafe(64))"`） | 开发默认值在生产拒绝启动 |
| `AUDIT_IP_HASH_KEY` | ✅ | 另一条独立长随机串 | 审计 IP 哈希专用；不得复用 `DJANGO_SECRET_KEY`，轮换不回填旧记录 |
| `DJANGO_ALLOWED_HOSTS` | ✅ | `<app>.up.railway.app`（加逗号可多域名） | DEBUG=False 下严格校验 Host |
| `DATABASE_URL` | ✅ | `${{Postgres.DATABASE_URL}}` | Railway 引用语法；settings 内置解析（含 URL 编码密码与 `?sslmode=` 参数），本地 compose 的 `POSTGRES_*` 组件方式继续可用且被它覆盖 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | ✅ | `https://<app>.up.railway.app` | HTTPS 表单 CSRF 校验 |
| `DJANGO_USE_WHITENOISE` | ✅ | `true`（whitenoise 已在 requirements.txt，见 §2） | 包缺失时启动即报错 |
| `SEC_USER_AGENT` | ✅ | `真实姓名 联系邮箱`（如 `Alice Zhang alice@example.com`） | SEC 访问政策要求可识别 UA；缺失时 SEC 同步无法合规运行 |
| `EMAIL_HOST` | ✅ | `smtp-relay.brevo.com` | 缺失时发送命令领取任何通知前报错退出（ADR-027） |
| `EMAIL_PORT` | ✅ | `587` | Brevo SMTP（STARTTLS） |
| `EMAIL_HOST_USER` | ✅ | Brevo 登录邮箱 | |
| `EMAIL_HOST_PASSWORD` | ✅ | Brevo SMTP key | 只存 Railway Variables，不进仓库 |
| `EMAIL_USE_TLS` | ✅ | `true` | 与 `EMAIL_USE_SSL` 互斥 |
| `DEFAULT_FROM_EMAIL` | ✅ | 已验证的 Brevo 发件人 | |
| `SEC_MAX_REQUESTS_PER_SECOND` | 建议 | `4`（上限 9） | SEC 公平访问上限跨机器合计 10 r/s；单实例部署保持默认 |
| `DJANGO_SECURE_SSL_REDIRECT` | 可选 | 默认 `false` | 平台边缘已强制 HTTPS；如需应用层再强制再开启 |
| `DJANGO_SECURE_HSTS_SECONDS` | 可选 | 默认 `3600` | 生产默认启用 1 小时 HSTS |
| `GUNICORN_WEB_WORKERS` / `GUNICORN_TIMEOUT_SECONDS` | 可选 | `2` / `60` | 512MB 套餐内存紧张时可降为 `1` |
| `RAW_DATA_MAX_PAYLOAD_BYTES`、`EARNINGS_CALENDAR_STALE_AFTER_SECONDS`、`REFERENCE_CALENDAR_STALE_AFTER_HOURS`、`GITHUB_URL` | 可选 | 保持默认 | 与本地一致；`GITHUB_URL` 留空用上游仓库 |

生产安全项（`SECURE_PROXY_SSL_HEADER`、`SESSION_COOKIE_SECURE`、
`CSRF_COOKIE_SECURE`、HSTS）在 `DJANGO_ENV != development/test` 时自动生效，
无需手动设置。

## 5. Cron 清单（频率按 docs/data-sources.md §7）

全部使用 Django management command；**未部署 Celery/Redis**，每个 Cron 服务
是一个短生命周期容器，跑完即退出。

| 任务 | Cron 表达式（UTC） | Custom Start Command | 并发与超时 |
|---|---|---|---|
| 通知发送 | `*/5 * * * *` | `python manage.py send_pending_notifications` | 领取用 `select_for_update(skip_locked)`，并发命令不会领取同一行（ADR-027 §7）；平台上一轮未结束会跳过本轮，与锁语义一致 |
| 提前一天提醒入队 | 每小时：`0 * * * *` | `python manage.py enqueue_advance_reminders` | 幂等；按美东自然日锚（ADR-024）到期才生效，重跑零重复 |
| SEC 文件同步 | 每 10 分钟：`*/10 * * * *` | `python manage.py sync_sec_filings --source-key sec_edgar` | `(source, job_type)` PostgreSQL 运行锁防重叠，重叠与陈旧运行有恢复语义；SEC 内置限速（默认 4 r/s） |

**刻意不注册 cron 的项**：

- **Alpha Vantage 财报日历（canonical 与 reference）**：同步编排目前只有
  service 层入口，没有 management command（PR #51 交付物不含命令）；data-sources
  §7 的「每日一次」待后续补一个薄命令层再注册。当前产品不依赖日历 cron。
- **IR 确认**：`LIVE BLOCKED`（ADR-022），无 live Provider。
- **指数成分/公告**：来源与许可未确认（fixture-first，ADR-005），零网络。
- **fixture-first 管线**：设计上离线，只用于测试与 replay，不属于生产调度。
- **健康检查命令**：`/health/`、`/health/data/` 是页面探针（7.3），浏览器或
  平台监控访问即可；MVP 不做独立健康 cron。

平台侧超时说明：Railway 不强杀运行中的 cron 运行（上一轮未结束则跳过下一轮），
因此**正确性由应用层锁保证**（通知 skip_locked、SEC 运行锁），且三个命令都是
分钟级完成的短任务；`EMAIL_TIMEOUT=15s`、Provider 超时/重试沿用既有内置值。

## 6. 迁移与发布步骤

- 日常发布 = push 到 main → Railway 自动构建部署；web 启动命令先
  `migrate --noinput` 再启动 gunicorn，单实例无滚动窗口。
- **大迁移窗口**：涉及长锁/重写数据的迁移时，先在 Railway 暂停三个 cron
  （清空 Cron Schedule 或删停服务），部署完成后再恢复——与 architecture §9.2
  「同步命令需兼容前后一个 schema 版本，否则暂停 Cron」一致。
- cron 服务部署同一镜像：push 后三个 cron 服务也会各自重新部署，下次按表
  触发即用新代码。
- 数据库迁移全部随代码提交（AGENTS.md §5），Railway 部署日志中可见
  `Applying ...` 序列；`makemigrations --check` 在 CI/verify.sh 已把关。

## 7. 备份与恢复

**策略（ADR-028）**：备份每日、保留 30 天；恢复演练至少一次。

1. **开启平台备份**：Railway Postgres 服务 → Backups → 启用 Daily（留 6 天）+
   Weekly（留 27 天）+ Monthly（留 89 天）。叠加后任意时间点在 6 天内有日级
   快照、27 天内有周级、89 天内有月级快照，满足「每日、保留 30 天」的产品
   目标（逐日 30 天保留需自建对象存储管线，alpha 不做）。
2. **每周异地逻辑备份（推荐）**：本地执行
   `BACKUP_DATABASE_URL=<Railway 的 DATABASE_PUBLIC_URL> ./scripts/backup-db.sh --url`
   （需在 Postgres 服务 Networking 里开启 Public Access；用后可关闭），产物
   存到本地 `backups/`（gitignored）。
3. **恢复演练（已执行）**：
   - 自动化脚本 `scripts/db-restore-drill.sh`：投掷 PostgreSQL 17 容器 →
     全量迁移 + 种子数据 → `pg_dump` → 还原到第二个空库 → 比对表清单与逐表
     行数 → Django 校验迁移与数据可读。
   - **2026-10-25 本地演练通过**：42 张表清单与行数完全一致，0 个未应用
     迁移，种子 superuser 经 ORM 可读（产物 `backups/restore-drill-20261025T065613Z.sql`）。
4. **平台快照恢复**：Postgres 服务 → Backups → 选日期 → Restore（原卷保留、
   新卷接管，确认后 Deploy）。仅限同一 project/environment。
5. **逻辑恢复**：`scripts/restore-db.sh --file <dump> --url` 或对本地 compose
   目标 `--file <dump>`；脚本强制 `--i-know-this-wipes-the-target` 防误删。
   灾难恢复优先用平台快照/PITR（Render 文档同样提示：导出恢复进**空库**）。

## 8. HTTPS 与域名

- Railway 生成的 `*.up.railway.app` 域名自动带证书，边缘强制 HTTPS；
  settings 在生产环境自动设置 `SECURE_PROXY_SSL_HEADER` 与安全 Cookie。
- 自有域名：web 服务 → Settings → Networking → Custom Domain，按提示加
  CNAME；证书自动签发。记得把新域名加进 `DJANGO_ALLOWED_HOSTS` 与
  `DJANGO_CSRF_TRUSTED_ORIGINS`。

## 9. 8.2 平台实测清单（本阶段的「经平台实测」验收项）

8.1 只交付配置与文档，以下各项部署后逐项打勾并记录：

- [ ] 三个 cron 服务按表真实触发（Railway Deployments 时间戳间隔 ≈ 表达式）；
- [ ] `send_pending_notifications` 观察连续 3 次 × 5 分钟间隔；有 pending
      通知时端到端收到 Brevo 邮件（可先 `railway ssh ... sendtestemail` 验证 SMTP）；
- [ ] `sync_sec_filings` 连续 2 轮 10 分钟间隔，第二轮 `unchanged` 计数无重复
      Filing（幂等），`/health/data/` 的 SEC 路由新鲜度进入 15 分钟目标；
- [ ] cron 运行时长（从 Deploy Logs 起止时间）≪ 5 分钟间隔，无跳过堆积；
- [ ] Railway Backups：手动触发一次备份并 Restore 到新卷，web 服务重连后
      `/health/` 正常；
- [ ] 上线后 24 小时观察 `/health/data/` 无异常 stale。

## 10. 已知限制与观察点

- **P0/P1 通知 5 分钟目标贴地**：cron 每 5 分钟一次 + Railway 执行漂移，
  端到端延迟可能周期性略超 5 分钟（ADR-004 的 P0/P1 目标按可测样本 ≥95%
  评估，见最终报告的 ADR-004 审计）。8.3 评审若数据显示不达标，修订路径：
  缩短通知 cron 间隔需同时修订 ADR-027 §7 或迁移到支持更高频率的平台。
- **Railway cron 表达式约束**：最小间隔 5 分钟、跨小时步进需整除（如
  `*/7` 非法）；`*/5`、`*/10`、`0 *` 均合法。
- **Railway Postgres 版本**：以开通时平台默认版本为准（当前默认 17，与本地
  compose 一致）；升级走 Railway 的 in-place upgrade 流程。
- **账号删除 / watchlist 匿名化**：延至 8.4 前另议（ADR-028 §6），不影响
  alpha 部署。

## 11. Alpha 时间线备注

产品负责人 2026-10-25 拍板：v0.1.0-alpha.1 部署后**自用 4–8 周**，再进入
8.3 alpha 退出评审；8.3 的验收标准本身不变（连续 14 个自然日门槛包含在该
窗口内），此项仅是排期备注。
