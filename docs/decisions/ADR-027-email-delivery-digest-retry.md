# ADR-027：邮件投递、每日摘要桶与失败重试契约

- 状态：已接受
- 日期：2026-10-25
- 决策者：产品负责人（D1–D3 已于 2026-10-25 拍板，本文落档；D4/D5 为工程实现决策，
  按 PRD/ADR-025/architecture §5.5 权威推导并在本文规范化）
- 影响阶段：6.4（邮件投递、摘要与重试）
- 关闭：`docs/development-roadmap.md` §4「邮件服务、摘要时间、重试规则 | 6.4 前」；
  `docs/data-model.md` §9.1 的 digest_bucket 与摘要字段预留；PRD §19.2 的实现选型
  （通用 SMTP，服务商部署时再选）

## 1. 背景

Stage 6.2/6.2W/6.3 交付了通知入队（transactional outbox）、三处接线与站内页面；
Notification 表以 `pending` 行充当持久待发送队列。6.4 需要投递 relay：把 pending 行
变成邮件/站内送达记录，含发送前复查（PD-4，ADR-025 §4 的执行者）、每日摘要与
失败重试。产品负责人 2026-10-25 拍板三项决策（D1–D3），本文固化。

## 2. D1：邮件服务 = 通用 SMTP 适配器（产品负责人拍板）

- 使用 Django 原生邮件设施（`django.core.mail` SMTP backend），**零新依赖**；
- 凭据与主机只从环境变量读取：`EMAIL_HOST`、`EMAIL_PORT`、`EMAIL_HOST_USER`、
  `EMAIL_HOST_PASSWORD`、`EMAIL_USE_TLS`、`EMAIL_USE_SSL`、`EMAIL_TIMEOUT`、
  `DEFAULT_FROM_EMAIL`，遵循 settings.py 既有 env 模式；
- 服务商（Resend/SendGrid/SES/其他 SMTP，PRD §19.2 候选）部署时再选，本期不绑定；
- 测试一律使用 notifications 内的 fake 发送器，不访问网络、不发真实邮件；
- 配置缺失的行为：发送命令启动时若 SMTP 发送器缺少 `EMAIL_HOST` 或
  `DEFAULT_FROM_EMAIL`，在领取任何通知之前以 CommandError 拒绝运行并输出原因
  （不产生 attempt、不迁移状态），操作者修复配置后重跑即可；Web 进程不受影响。

## 3. D2：每日摘要 = 用户本地时间每日一桶，默认 08:00（产品负责人拍板）

- P3（摘要级）通知按 **（用户 × 本地日期桶）** 聚合投递：邮件渠道合并为一封
  摘要邮件；in_app 渠道在同一时刻逐行迁移到 sent（站内行入队即已可见，
  迁移只是投递状态的收口，见 D5）；
- `UserNotificationPreference.digest_time_local` 落库为可空 `TimeField`，
  NULL = 默认 08:00 生效；`/settings/notifications` 增加摘要时间输入；
- Russell 2000 周摘要与 `digest_weekday` 延后到二期，本轮不落库、不预置；
- 桶的计算（工程规范）：
  - 用户时区取 `User.timezone`；为空时回退 **America/New_York**（与 ADR-024
    单一确定性锚的哲学一致：无用户时区时也要有确定桶，不用「浏览器时间」
    这种服务器不可知的东西）；
  - 入队时为 P3 行计算 `digest_bucket` = **下一次尚未到达的摘要时刻所在本地日期**：
    本地当前时刻早于当日 `digest_time_local` 则为当日，否则为次日。这样入队后
    跨本地午夜的重放仍落在同一桶，不会因重跑产生第二行；
  - 摘要投递到期判定 = `combine(digest_bucket, digest_time_local, 用户时区) <= now`
    （zoneinfo 处理 DST；美国 DST 切换发生在 02:00–03:00，08:00 摘要时刻在切换日
    依然存在且唯一）；到期前 P3 行不被领取。

### 幂等键 v2（ADR-025 §7 预留方向的落地）

- **仅 P3 行**的幂等键升级为
  `sha256("v2" | v1 全部输入 | digest_bucket.isoformat())`；
- P0–P2 行的键与 v1 **逐字节相同**（ADR-025 冻结的语义与非幂等重放行为不变）；
- 同一 P3 occurrence 在同一桶内重跑/并发入队 → 同键一行（「同桶重跑不重复发送」）；
- 已知边界（接受并记录）：跨桶重放同一 P3 occurrence（例如摘要已发送后旧事实重放）
  会产生新桶的新行并出现在下一天摘要中；P3 是摘要级信息，重复一条可接受，
  且与 v1「已投递状态冻结」相比仅在摘要级放宽。另一边界：入队时为 S/A 级、
  之后用户把公司移出自选再重放，可能同时存在旧 P3 行与新 P0/P1 行；
  发送前复查（D4）只抑制 S/A 源行，不合并去重，属可接受的边缘情形。

## 4. D3：重试规则（产品负责人拍板）

- **临时失败**：网络错误、连接失败、超时、限流（4xx throttling，含 421/429 类）、
  以及除「无效收件人 / 垃圾邮件拒收」之外的提交期 5xx——总计最多 **3 次尝试
  （含首次）**，失败后间隔**固定 15 分钟**再试（不做指数退避，个人规模无需）；
- **永久失败**：硬退信（收件人不存在 5.1.x）、无效收件人（RCPT 阶段全部拒收）、
  垃圾邮件/内容拒收（5.7.x）——**立即停止**并记录错误分类与脱敏原因；
- 凭据错误（SMTP AUTH 失败）按永久失败处理：重试相同错误凭据只会制造噪音，
  修复配置后由操作者/管理员重试路径处理（MVP 无自动重发，failed 为终态，
  管理员修正属后续阶段）；
- 不可分类的异常记 `outcome=unknown`，按临时失败参与有界重试（3 次上限兜底）；
- 第 3 次尝试仍临时失败 → `status=failed` 终态；每次尝试（含被抑制前的所有
  发送尝试）追加一行 `NotificationDeliveryAttempt`，历史只增不改；
- 摘要邮件的重试作用于桶内全部成员行（整桶回 pending，15 分钟后整桶重试）；
- 「外部邮件已接受但本地写结果失败」的供应商侧重复残余风险维持
  architecture §5.5 的既定结论：记录该风险，MVP 不引入分布式事务。

## 5. D4：发送前复查 = PD-4 执行者 + 三门复查（工程实现决策）

发送命令领取 pending 行后、投递前执行复查（ADR-025 §3：发送前复查才是
抑制的最终权威）。为使「watchlist 相关事件」可执行，入队时记录
`origin_company_level`（S/A/B/C/none = 当时该用户相对该公司的层级，none =
full_exit 对非自选用户的特例）。复查规则：

1. **渠道门**：`UserNotificationPreference` 当前关闭该渠道 →
   suppressed（`channel_disabled`）；
2. **公司门 / PD-4 成员复查**：
   - `origin ∈ {S, A}`：公司当前不在该用户有效自选（active 条目）中，或存在
     active 条目但 `alerts_enabled=False` → suppressed（`company_alerts_disabled`）；
   - `origin ∈ {B, C}`：无自选关系，不复查成员；但若用户后来把公司加回自选并
     关闭公司提醒（active 条目 `alerts_enabled=False`）→ 同样 suppressed；
   - `origin = none`：投递（入队时即无关系，full_exit 的池内最后一条事实）；
   - `origin` 为 NULL（6.4 之前的历史行）：公司当前既不在自选也不在任何基础指数
     （对该用户完全出池）→ suppressed，否则投递；
3. **规则门不复查**：规则级 disable 在入队时已生效，发送时不重新解析规则链
   （需要 lead 上下文重建，收益低）；改规则后旧 pending 行仍投递，新 occurrence
   按新规则入队——与 ADR-025 D2「pending 不降级」一致；
4. 抑制不产生 attempt 行（没有发生发送尝试），只迁移 `status=suppressed` +
   原因码（沿用 6.2 模型的三个原因码）；D2 和和解语义保持：同一 occurrence
   再次 dispatch 时 suppressed 行可翻回 pending。

## 6. D5：in_app 渠道的状态语义（产品负责人 2026-10-25 拍板）

- 发送命令统一领取两渠道的 pending 行；in_app 行复查通过即迁移
  `status=sent`、写 `sent_at` 与一条 `outcome=accepted` 的 attempt
  （站内无外部系统，「投递完成」= 发送流程处理完毕；可见性自入队起已成立，
  见 architecture §5.5.5，本决策不改变可见性）；
- 复查不过 → suppressed + 原因码（与 email 一致）；已读状态完全独立不变；
- P3 的 in_app 行与 email 行在同一摘要时刻迁移（邮件渠道聚合为一封，
  站内渠道逐行 sent）；
- 「已读驱动 sent」被否决：与已合并的 6.3 验收标准「已读与发送状态分离」
  和 architecture §5.5.5 冲突。

## 7. 领取与并发（工程实现决策）

- 领取 = `select_for_update(skip_locked=True)` + 领取时再校验状态
  （`pending` + `next_attempt_at` 到期 + 非 P3 或摘要桶到期），两个并发命令
  不会领取同一行；领取即迁移 `processing` + `claimed_at`；
- 处理完成（sent/failed/suppressed/回 pending 重试）各自落库后释放锁语义
  （事务提交）；崩溃残留的 `processing` 行不会被自动重领（MVP 不做超时回收，
  由管理员核查；healthcheck 的积压可见性属 7.3）；
- 发送命令每 5 分钟由 cron 驱动（注册留 8.1），支持 `--dry-run` 全程回滚。

## 8. 调研借鉴（设计层面，不复制代码，不引入依赖）

- **Medium Engineering「Scaling email infrastructure for Medium digest」**：
  按用户时区分桶发送摘要、避免按整点假设时区偏移——D2 的分桶先例；
- **SaaS 每日报告的 per-user timezone 调度惯例**（如 calendar-first daily
  reports 模式）：单一调度器 + 应用层按用户 IANA 时区判定到期，去重键
  `(user, local_date, digest_type)`——D2 到期判定与桶幂等的直接依据；
- **邮件服务商退信分类惯例**（Mailgun/Nylas/HubSpot 等的 hard vs soft bounce
  文档）：hard bounce（5.1.x 无效收件人、5.7.x 拒收）立即抑制、soft bounce
  有界重试、以实际 SMTP 增强状态码而非术语分类——D3 分类的先例；
  「hard/soft bounce 并无 RFC 定义、各家 ESP 不完全一致」（TrueGuard）佐证了
  必须写死本项目自己的分类表。

## 9. 备选方案与拒绝理由

- **第三方邮件 API SDK（Resend/SendGrid SDK）**：引入新依赖 + 提前绑定服务商；
  Django 原生 SMTP 足以覆盖，服务商部署时再选；拒绝；
- **digest_bucket 现在并入所有键**：P0–P2 行无摘要语义，并入等于无谓地改变
  ADR-025 冻结的 v1 键、制造历史行孤儿；仅 P3 行升 v2；
- **指数退避重试**：个人规模、单实例 cron 下固定 15 分钟足够，少一个可调参数；
- **processing 行超时自动回收**：需要心跳/租约字段与时钟假设，MVP 由管理员
  核查更简单（7.3 的健康检查可见性兜底）；
- **发送时重新解析规则链**：见 D4.3，收益低于复杂度。

## 10. 测试要点

- fake 发送器全流程：领取 → 复查 → 投递 → 状态迁移 + attempt + 快照；
- 重试三档（1 次失败后成功 / 3 次全失败 / 永久失败立即停止）与 15 分钟退避；
- 并发领取互斥（两个发送者不领取同一行）；
- 摘要桶：到期才领、同桶重跑不重发、跨时区（UTC/美东/东京）与 DST 切换日、
  `digest_time_local` 修改生效、幂等键 v2 同桶去重；
- 复查：S/A 源行移除自选后抑制、B/C 源行不受移除影响、渠道关闭抑制、
  suppressed 原因码正确；
- in_app：复查通过即 sent + attempt、P3 站内行等摘要时刻、已读独立；
- 设置页：摘要时间保存/校验/CSRF/权限；
- 「同一输入连续执行两次」：发送命令重跑零重复（AGENTS.md §8）；
- 正文含 PRD §11 各事件类型的指定字段与来源链接、美东 + 用户本地双时间。

## 11. 影响与未决

- 迁移 `notifications/0004`：`notification` 表新增可空列
  （`scheduled_at`/`claimed_at`/`sent_at`/`attempt_count`/`next_attempt_at`/
  `subject_snapshot`/`body_snapshot`/`last_error_code`/`last_error_message`/
  `digest_bucket`/`origin_company_level`）、`usernotificationpreference` 新增
  可空 `digest_time_local`、新表 `notificationdeliveryattempt`；不改既有列，
  回滚 = 删表/删列；
- cron 注册、生产邮件服务商选择与 smoke test 属 8.1/8.2；
- Russell 2000 周摘要（`digest_weekday`）二期；
- 管理员对 failed 行的修正/重发工具不在本期（failed 终态 + 审计可见）；
- PRD §11.3 的「财报新闻稿 / IR 页面」链接：当前数据模型无对应 URL 字段，
  正文呈现已有事实（公司、财季、发布时间、关联 8-K/10-Q/10-K 链接、来源链接），
  字段补齐属 earnings 数据模型扩展，届时正文模板自然补上。

## 参考

- Medium Engineering: *Scaling Email Infrastructure for Medium Digest*
- Mailgun: *Email Bounces: What to Do About Them*；Nylas: *Soft Bounce vs Hard
  Bounce*；HubSpot: *Hard vs Soft Bounce*；TrueGuard: *Hard Bounce vs Soft Bounce*
- ADR-024（矩阵与抑制门）、ADR-025（outbox、幂等键、PD-4、和解语义）、
  ADR-026（接线点）、architecture.md §5.5/§6、PRD §9.5/§11/§12.5/§17.3
