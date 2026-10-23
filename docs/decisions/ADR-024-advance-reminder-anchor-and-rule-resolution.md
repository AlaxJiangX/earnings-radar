# ADR-024：提前一天提醒的美东自然日锚定与提醒规则解析

- 状态：已接受
- 日期：2026-10-24
- 决策者：产品负责人（D1 时区/DST 语义已拍板，本文落档）；D2/D3 为工程实现决策，
  按 PRD §9/§10 权威推导并在本文规范化
- 影响阶段：6.1（提醒规则与优先级）、6.2（Notification 入队）、6.4（送达窗口）
- 关闭：`docs/development-roadmap.md` §4「提前一天的时区/DST 语义」；
  `docs/data-model.md` §14 待确认 #7（规则覆盖语义）与 #8（提前一天时区语义）

## 1. 背景

PRD §11.1 的「提前一天提醒」（示例主题「NVDA 将于明日美股盘后发布财报」）需要一个
确定的日期语义：提醒日按哪个时区的自然日计算、DST 切换日如何处理。PRD §9.2 把
「用户主动关注」排在「信息可靠性」之上，但 §9.4 的 P0–P3 清单是离散条款，不是可
执行矩阵；data-model §14 #7 另有「用户级与公司级 ReminderRule 覆盖还是叠加」待确认。

产品负责人 2026-10-24 拍板 D1；本轮不得重新决策。

## 2. D1：提前一天 = 美东自然日的前一自然日

```text
提醒日 = 财报显示日（America/New_York 自然日）− 1 个自然日
```

- 财报显示日与 `earnings.presentation.business_date()` 同一锚
  （`MARKET_TIMEZONE = America/New_York`）；
- 「一天」是**名义日历日**，不是 86400 秒的固定偏移：跨 DST 切换时实际间隔为 23
  或 25 小时，转换全部交给 `zoneinfo`；
- 用户时区只影响显示与送达窗口，不改变提醒日；送达窗口的具体时刻（本地几点）属
  6.4 的「摘要时间」决策，本文不定义；
- 实现：`notifications.services.lead_time` 纯函数
  （`market_date_from_utc` / `reminder_date_for_lead` / `is_advance_reminder_due`），
  领域服务不读 wall clock，`market_date` 由调用方传入；lead 只允许整天（分钟的整数
  倍 = 1440 的整数倍），非整天 lead 拒绝而非四舍五入。

## 3. D2：S/A/B/C 与 P0–P3 规范矩阵

公司级（PRD §9.1）：S = 重点自选；A = 普通自选；B = 非自选 LARGE 指数
（Dow 30 / S&P 500 / Nasdaq 100 任一）；C = 非自选 SMALL（Russell 2000）。
既非自选也无指数成员 = 监控池外，优先级函数拒绝而不是猜一级。

事件级矩阵（days_until 为美东自然日差；「24 小时内」≡ days_until ∈ {0, 1}，与 D1
锚定衔接：当天发布必然不足 24 小时，旗舰「明日发财报」提醒恰在 days_until=1 触发，
对应 PRD §9.4 P0/P1 的「24 小时内发财报」条款）：

| 事件类型 | S | A | B | C | 备注 |
|---|---|---|---|---|---|
| earnings_released | P0 | P1 | P1 | P3 | 「重要公司」在 MVP 无市值/热度权重判据（§9.2 明确排除热度），按 LARGE 成员实现 |
| date_changed | P0 | P0 | P3 | P3 | |
| filing | P0 | P0 | P3 | P3 | |
| index_transferred | P0 | P0 | P1 | P3 | 偏移必涉及 LARGE 指数（仅一个 SMALL 指数，不存在 SMALL→SMALL） |
| index_added / index_removed | P1（LARGE）/ P3（SMALL），与公司级无关 | | | | P1 条款主语是「大型指数公司」；P0 只为自选股「偏移」保留 |
| full_exit | P0 | P0 | P3 | P3 | 非自选公司退出全部基础指数即出池，§9.5 归摘要/网页 |
| earnings_upcoming days 0–1 | P0 | P1 | P2 | P3 | B/C 且未确认（estimated）→ P3 |
| earnings_upcoming days 2–3 | P1 | P1 | P3 | P3 | |
| earnings_upcoming days 4–7 | P2 | P2 | P3 | P3 | |
| earnings_upcoming days >7 | P3 | P3 | P3 | P3 | |

PRD 未逐格写明的两处按 §9.2/§9.5 推导并在此固定（不是新增产品行为）：

1. **普通自选股财报发布 = P1**：P0 明确限定「重点自选股财报发布」；§9.5 普通自选
   「重要事件实时提醒」+ §9.3 紧迫度第 1 位，且不得高于 S 级，取 P1；
2. **estimated 日期只降非自选**：P3 条款「尚未正式确认的预计日期」按 §9.2
   「用户主动关注 > 信息可靠性」只作用于 B/C；S/A 保持其紧迫度档位。

实现为纯函数 `notifications.services.priorities`：公司级由「自选事实 + 指数事实」
输入，事件级对不适用的事实参数 fail closed（缺 days_until/is_confirmed/index_is_large
或对固定事件误传参数均抛错）。

## 4. D3：规则解析语义（覆盖、门与内置默认）

```text
渠道总开关（用户级）  ─┐
                      ├─ AND 抑制门，独立于规则链
公司总开关（自选条目）─┘

事件类型规则（每 event_type + channel）：公司级规则集 ──存在──> 整体替换
                                                      └─不存在─> 用户默认规则集
                                                                └─不存在─> 内置默认（启用）
```

- **整体替换（replace）**：公司级 ReminderRule 存在时，该 (event_type, channel)
  的用户默认规则集（含所有 lead 档）全部失效，不用公司规则与用户默认合并/叠加——
  采用 Google Calendar `defaultReminders` vs `overrides` 的替换语义；
- **门不进入替换链**：渠道开关（`UserNotificationPreference.email_enabled /
  in_app_enabled`，缺行 = 全开）与公司总开关（active `WatchlistItem.alerts_enabled`，
  无 active 条目 = 无门）各自独立抑制，借鉴 Grafana「抑制窗口不随内容覆盖链继承」
  的教训；被门抑制的决定保留规则原状态并给出原因码，供 6.2 记录
  suppressed 原因；
- **内置默认**：无任何规则行时按「全部 MVP 提醒类型启用」处理；
  `earnings_upcoming` 的内置 lead 取 preference 的 `default_lead_time_minutes`
  （默认 1440），其余类型 lead 恒为 0（数据库 check 强制）；
- 唯一键 `(user, company, event_type, channel, lead_time_minutes)`（company 为
  NULL 时按 NULLS NOT DISTINCT 视为相等），同一 upcoming 事件允许多档 lead 共存，
  每档独立开关。

## 5. 调研借鉴（设计层面，不复制代码，不引入依赖）

- **RFC 5545 §3.8.6.3 VALARM / WS-Calendar 名义时长**：`TRIGGER:-P1D` 是事件时区
  下的名义日历日（23/24/25 小时），不是固定秒数偏移——D1 的直接标准先例；
- **Google Calendar API reminders**：日历级 `defaultReminders` 与事件级
  `overrides`（`useDefault=false` 时整体替换、不合并）——D3 替换语义的成熟先例；
- **Grafana notification policies**：嵌套策略继承内容覆盖，但 mute timings 不随
  继承链传播、须逐层显式——D3 把抑制门放独立层、不并入替换链的教训来源。

## 6. 备选方案与拒绝理由

- **用户本地时区锚定**：同一财报对不同用户产生不同提醒日，且提前一天提醒与
  「明日盘后」的产品文案（PRD §11.1）在非美东用户上失真；拒绝；
- **固定 24 小时偏移**（release_at − 24h 触发）：跨 DST 为 23/25 小时的名义日
  语义漂移，且 date-only 事件没有可用的 release_at；拒绝；
- **公司规则与用户默认叠加合并**（并集）：同一事件可能同时命中两级规则产生重复
  通知，关闭语义无法表达「该公司只提前 1 天、不要 3 天」；拒绝；
- **抑制门并入替换链**（公司规则可覆盖渠道总开关）：单公司规则无法重新打开用户
  已全局关闭的渠道，且与「总开关」的 PRD §10.6/§10.9 语义冲突；拒绝。

## 7. 测试要点

- DST 边界：2026-03-08（spring forward）、2026-11-01（fall back）切换日前后 UTC
  时刻 → 美东自然日断言；跳过的 02:00–03:00 与重复的 01:00–02:00 均落在正确的
  market date；切换日当天作为提醒日的 nominal-day 断言；用户本地（Asia/Tokyo）
  日期与美东锚不同的对照；
- 幂等/稳定：优先级矩阵全输入两遍结果一致；策略解析同输入重复调用结果一致；
- 矩阵：固定事件 × 4 级、index 事件 × large/small、upcoming 档位 × 4 级 ×
  confirmed/estimated 全格断言；不适用事实参数抛错；
- 规则：8 种 MVP 事件类型各至少一条可测试规则；null-safe 唯一键、lead check、
  渠道门、公司总开关（开→关→开、移除后无门）、公司整体替换用户默认、跨用户隔离。

## 8. 影响与未决

- 6.1 只交付模型、纯计算与解析服务；Notification 入队/去重/发送是 6.2，
  页面是 6.3，邮件与摘要时间是 6.4；
- ADR-023 §13 PD-4（移除自选时是否取消已入队通知）留给 6.2，本文不定义；
- `UserNotificationPreference` 的 digest 字段（摘要时间/星期）随 6.4 摘要决策
  一并落库，6.1 不预置空字段；
- 迁移只新增两张表（`notifications` 0001），不改既有表；回滚 = 删除两表。

## 参考

- RFC 5545 *Internet Calendaring and Scheduling Core Object Specification*
  (iCalendar)，§3.8.6.3 VALARM TRIGGER；
- OASIS WS-Calendar Specification v1.0（nominal durations across DST）；
- Google Calendar API：Reminders and notifications（defaultReminders / overrides /
  useDefault）；
- Grafana 文档：Configure notification policies（inheritance 与 mute timings）。
