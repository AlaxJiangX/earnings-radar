# ADR-019：Alpha Vantage Free Mode A Reference Provider License Gate

- 状态：已接受（gate 结论：**BLOCKED — PROVIDER CLARIFICATION REQUIRED**）
- 日期：2026-09-29
- 决策者：产品负责人
- 评估与起草：Codex（按 Stage 4.2F-A Mode A License Gate 执行）
- 评审基线：分支 `codex/4.2f-a-planning-gate`，HEAD
  `f7a7347abc5ed72886a20afb8426a870d54558dc`；`origin/main`
  `2b351cca85fa1e2bc1d0f866b6b35205986ce0c4`
- 范围：只评估 Alpha Vantage Free 作为 ADR-018 **Mode A reference-only Provider** 的
  许可 / 数据权利；不实现代码、不加 key、不改 schema。

## 1. 背景

ADR-016 已拒绝 Alpha Vantage Free 作为 4.2F canonical primary Provider。本 gate 回答的是
另一个问题：它能否作为 ADR-018 下的 Mode A reference-only Provider 被批准。

本 gate 评估的当前用例仅限：

```text
personal / private
single-user / internal
non-commercial
reference / estimated earnings calendar
不公开再分发、不销售、不提供给第三方用户
```

架构需要的操作：fetch `EARNINGS_CALENDAR` → 持久化 `RawDataRecord` → 长期保留历史 raw →
offline replay（network fetch = 0）→ 从 persisted raw 确定性派生 reference rows → 私有
展示 → 保存 parser/projection 元数据与 diagnostics → 比较 freshness → 保留来源可审计性。

Zero Data Cost 定义沿用 ADR-017，不因本 ADR 扩展；`Zero Data Cost != Zero Total Cost`。

## 2. 审查的官方来源

只使用官方来源形成结论；第三方博客、论坛或 wrapper 解读未用于法律判断。

| 来源 | 标题 / 文档 | 日期信息 | 相关部分 | reviewed_at |
|---|---|---|---|---|
| `https://www.alphavantage.co/terms_of_service/` | TERMS OF SERVICE（4 页 PDF） | 正文无显式版本或生效日期；PDF metadata CreationDate 2022-12-25，ModDate 2026-08-27 | §2 Grant of License；§3 EULA；§4 Use Restrictions；§5 IP；§6 License Term；§7 Termination；§13 Modifications；§21 Entire Agreement | 2026-09-29 |
| `https://www.alphavantage.co/documentation/` | API Documentation，Earnings Calendar 章节 | 抓取日 2026-09-29 | "next 3, 6, or 12 months"；market-wide list；"Claim your free API key here"，无 premium 标记 | 2026-09-29 |
| `https://www.alphavantage.co/support/` | Customer Support / FAQ | 抓取日 2026-09-29 | "lifetime access"；"25 API requests per day and unlimited API requests for verified open-source or educational projects"；wrapper open-source FAQ | 2026-09-29 |
| `https://www.alphavantage.co/premium/` | Premium API Key | 抓取日 2026-09-29 | majority endpoints free；超额或 premium endpoint 才需要付费计划 | 2026-09-29 |
| `https://www.alphavantage.co/realtime_data_policy/` | Realtime Data Policy | 抓取日 2026-09-29 | personal vs business/commercial 的通用语境；非 earnings calendar 专项 | 2026-09-29 |

关键 ToS 原文（节选）：

```text
§2.a  Alpha Vantage grants the right to install, use, access, display and run the
      software on any computer or mobile device ... that you own or control, for
      personal, non-commercial use, unless you and Alpha Vantage have agreed
      otherwise in writing ...
§2.a.i  commercial use 包括 "any purpose that goes beyond investment analysis,
      research, testing, monitoring, and any other activities that are private
      and individual in nature"
§2.a.iii commercial use 包括 "provide information ... that allows individuals or
      entities other than User to access information directly or indirectly"
§3    license 是 non-exclusive / non-sublicensable / non-transferable /
      non-assignable / revocable
§13   修改必须 "in writing and signed by an authorized representative"
§21   构成 entire agreement，取代此前的口头或书面约定
```

ToS 全文没有任何 caching、storage、retention、archive、redistribution、derivative data 或
offline replay 的明示条款。

## 3. 权利矩阵

分类规则：明确授权或清晰必要的推论 = YES；明确限制 = NO；沉默 / 模糊 = UNKNOWN。

| Right | 分类 | 官方证据 |
|---|---|---|
| ongoing free access | YES | Support："free stock API service ... 25 API requests per day"、"lifetime access"；Documentation：Earnings Calendar 可 claim free API key 且无 premium 标记；每天 1 次 reference fetch 在额度内 |
| automated private retrieval | YES | ToS §2.a 授权在自有 / 控制的 computer 上 use / access / run，用于 personal, non-commercial use；§2.a.i 明确把 monitoring / research 等 private、individual 活动列为非商业用途 |
| raw payload persistence | UNKNOWN | ToS 完全未提 cache / store / retain；§2.a 只列 install / use / access / display / run；§4 只限制 reverse engineering；§13 / §21 排除了用营销或支持文案补足未授予权利 |
| historical retention | UNKNOWN | ToS 未提允许多份历史响应保留或保留期限 |
| offline replay | UNKNOWN | ToS 未提允许对已保存数据离线再处理；§5.b 的 "User retains own data" 只是用户自有数据 IP 归属，不是 AV Content 的保存 / 再处理授权 |
| derived / reference projection | YES（仅读取时派生并私有展示） | 对获授权的 access / use / display 而言，解析必要字段是清晰必要的推论；持久化派生数据未被条款提及，属于 UNKNOWN，v1 不持久化派生行 |
| private / internal display | YES | ToS §2.a 明确授予 display，用于 personal, non-commercial use；单用户私有实例不触发 §2.a.ii / iii |
| audit metadata retention | YES | fetch 时间、request fingerprint、content hash、parser/projection 版本、raw position 与 diagnostics 属于用户自有运行元数据，不复制 AV Content；ToS 无限制条款；但不能用它补足 raw payload 持久化的 UNKNOWN |
| alerts / notifications | UNKNOWN | ToS 未针对提醒 / 通知作说明；不属于当前实现切片，不作为本 gate 的 blocker |

## 4. Commercial Boundary

```text
current single-user personal/private use =
  YES（ToS §2.a 明确允许的范围）

future multi-user / public / commercial use =
  REQUIRES AGREEMENT（ToS §2.a.ii / iii；需通过 premium@alphavantage.co 书面约定）
```

ToS 把"向 User 以外的人或实体直接或间接提供信息"定义为 commercial use。因此任何公开
展示、多用户实例、SaaS 或再分发都不属于本次批准范围。

## 5. Zero Data Cost 与 Entire-Agreement 影响

- 免费访问与额度：满足 Zero Data Cost 的访问条件（25 请求/天，1 请求/天足够）；
- §13 / §21：只有官方书面条款或签名书面澄清才可能把 UNKNOWN 升级为 YES；marketing、
  FAQ、support 文案不能作为授权依据；
- 本 gate 不发送澄清请求，也不改变任何 provider 的批准状态。

## 6. Gate Decision

按 ADR-018 与本 gate 的判定规则：

```text
required rights:
  ongoing free access              = YES
  automated private retrieval      = YES
  raw payload persistence          = UNKNOWN
  historical retention             = UNKNOWN
  offline replay                   = UNKNOWN
  derived / reference projection   = YES（读取时）
  private / internal display       = YES
  audit metadata retention         = YES

no required right = NO

Gate result = BLOCKED — PROVIDER CLARIFICATION REQUIRED
```

这不是 REJECTED：ToS 没有明确禁止保留 / replay；但在书面澄清把 UNKNOWN 变成 YES 之前，
Alpha Vantage Free 不得进入 production ingestion，4.2F-A implementation 保持 BLOCKED。
ADR-016 对 canonical provider 的拒绝不因此改变。

## 7. Provider Clarification Package

以下问题描述真实架构，均为 yes/no 问题；本轮不发送。

```text
1. May an individual free-API user store complete EARNINGS_CALENDAR API responses,
   together with local fetch/audit metadata (fetch time, content hash, parser
   version), in a private database for long-term personal use?
2. May the user retain multiple historical EARNINGS_CALENDAR responses over time
   (an append-only archive), rather than keeping only the latest response?
3. May the user later re-process those stored responses offline (re-parse and
   re-display them) without making another API call?
4. May the user derive reference fields (issuer symbol, estimated report date,
   session/time) from stored responses for private/internal display in a
   single-user application?
5. May the user display those derived reference rows to that same individual user
   in a private, non-commercial application that no other person or entity can
   access?
6. If the free API key is terminated or rotated, may the user continue to read and
   process responses already stored before termination, and does Alpha Vantage
   require deletion of stored responses at any point?
```

Future scope question（不阻塞当前 V1，供后续 open-source self-host 评估）：

```text
7. Do these permissions extend to an open-source, self-hosted application where each
   individual runs their own private instance with their own free API key and no
   data is shared between users?
```

## 8. 后果与后续

- Alpha Vantage Free = Mode A **PENDING CLARIFICATION**，未获批；
- 4.2F-A implementation 保持 BLOCKED；
- 若书面澄清对任一必需权利回答 NO，则升级为 REJECTED；
- 若书面澄清对所有必需权利回答 YES，可据此更新 ADR-019（或另开 ADR）并重跑 gate；
- 不实现 provider adapter、HTTP transport、parser、projection、command、UI、model、
  migration 或 runtime schedule。

## 参考

- `docs/decisions/ADR-016-alpha-vantage-free-provider-gate.md`
- `docs/decisions/ADR-017-zero-data-cost-reference-calendar.md`
- `docs/decisions/ADR-018-reference-calendar-4.2f-a-contract.md`
- `docs/data-sources.md` §2、§8
- `docs/development-roadmap.md` Stage 4.2F-A
