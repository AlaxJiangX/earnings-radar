# ADR-019：Alpha Vantage Free Mode A Reference Provider License Gate

- 状态：已接受（2026-09-30 resolution gate：**PASS**，仅限个人、私有、单用户 Mode A reference 用途）
- 日期：2026-09-29
- 决策者：产品负责人
- 评估与起草：Codex（按 Stage 4.2F-A Mode A License Gate 执行）
- 评审基线：分支 `codex/4.2f-a-planning-gate`，HEAD
  `f7a7347abc5ed72886a20afb8426a870d54558dc`；`origin/main`
  `2b351cca85fa1e2bc1d0f866b6b35205986ce0c4`
- 范围：只评估 Alpha Vantage Free 作为 ADR-018 **Mode A reference-only Provider** 的
  许可 / 数据权利；不实现代码、不加 key、不改 schema。

> 修订说明（ADR-020）：本 ADR 的 Mode A reference-only 许可结论不变。ADR-020 另行冻结
> Alpha Vantage Free v2 candidate-entry 技术契约；后续书面澄清（由产品负责人转述）已
> 覆盖同一 strictly personal/private/single-user/non-commercial 范围内的 normalized /
> candidate / canonical-pipeline storage。公开、多用户、商业与再分发仍不在批准范围内。

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
以下为 2026-09-30 resolution gate 的当前判定；2026-09-29 的未知项已由 §9 的
Alpha Vantage Support 书面回复澄清。所有 YES 仅适用于 §1 所述个人使用范围。

| Right | 分类 | 官方证据 |
|---|---|---|
| ongoing free access | YES | Support："free stock API service ... 25 API requests per day"、"lifetime access"；Documentation：Earnings Calendar 可 claim free API key 且无 premium 标记；每天 1 次 reference fetch 在额度内 |
| automated private retrieval | YES | ToS §2.a 授权在自有 / 控制的 computer 上 use / access / run，用于 personal, non-commercial use；§2.a.i 明确把 monitoring / research 等 private、individual 活动列为非商业用途 |
| raw payload persistence | YES | Support 对原始 `EARNINGS_CALENDAR` CSV/JSON 长期保存问题明确答 YES；仅限个人私有数据库 |
| historical retention | YES | Support 对保留多份历史 API 响应明确答 YES，并确认无额外保留期限 |
| offline replay | YES | Support 对无需再次调用 API 而离线读取、处理已保存响应明确答 YES |
| derived / reference projection | YES | Support 对从已保存响应派生字段并在私有单用户应用展示明确答 YES；4.2F-A v1 仍只在读取时投影，不持久化派生行 |
| private / internal display | YES | ToS §2.a 明确授予 display，用于 personal, non-commercial use；单用户私有实例不触发 §2.a.ii / iii |
| audit metadata retention | YES | fetch 时间、request fingerprint、content hash、parser/projection 版本、raw position 与 diagnostics 属于用户自有运行元数据；Support 已明确允许包含来源时间与 freshness metadata 的私有派生字段 |
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
- §13 / §21：一般营销、FAQ 或未核实身份的支持文案不能补足未知权利。本次依据的是
  从 ToS §19 所列 `support@alphavantage.co` 发出的、针对所述个人用途四项具体问题的
  直接书面确认；这是对现有个人使用范围的澄清，不据此修改合同或授予新场景的许可。
  回复没有双方签署的合同修改形式，因此不能据此扩大 §2.a 的许可范围；任何超出
  该范围的合同修改仍须满足 §13 的签署要求。这是对原 gate 所设“签名书面澄清”
  审查口径的显式限缩：本次只接受官方支持邮箱对现有个人许可的解释，不接受其
  作为新增合同权利的证据。
- 该回复没有扩大免费额度，也没有批准公开、多用户、商业使用或再分发。

## 6. Gate Decision

按 ADR-018 与本 gate 的判定规则：

```text
required rights:
  ongoing free access              = YES
  automated private retrieval      = YES
  raw payload persistence          = YES
  historical retention             = YES
  offline replay                   = YES
  derived / reference projection   = YES
  private / internal display       = YES
  audit metadata retention         = YES

no required right = NO

Gate result = PASS（仅限个人、私有、单用户、非商业 Mode A reference）
```

Alpha Vantage Free 获准作为 4.2F-A 的 Mode A reference-only Provider；这不是 4.2F-A
实现或上线验收。ADR-016 对 canonical primary Provider 的拒绝不因此改变。

## 7. Provider Clarification Package

以下为 2026-09-29 记录的原始澄清问题；2026-09-30 的实际邮件以四项问题询问，
并获得 §9 所记录的答复。第 6 项的终止/轮换后使用和第 7 项的开源自托管多实例
未被逐项回答，本 gate 不把这两种场景列入当前批准范围。保留原问题供历史追溯。

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

- Alpha Vantage Free = Mode A reference-only **APPROVED**，仅限个人、私有、单用户、
  非商业实例；不得将数据公开、再分发、销售或提供给其他用户。
- 转为公开、多用户、客户可访问、商业或再分发用途前，必须重新审查许可并取得相应协议；
  当前批准不得直接用于 PRD 中的公开页面或多用户服务。
- 4.2F-A implementation 尚未开始；未来实现仍须满足 ADR-018 的工程与测试验收。
- 4.2F-B canonical primary Provider 仍受 ADR-016 的拒绝约束；AV v2 candidate-entry
  路径已由 ADR-020 另行批准，个人用途许可已由后续书面澄清解决，公开 / 多用户 /
  商业仍保持 BLOCKED。
- 不实现 provider adapter、HTTP transport、parser、projection、command、UI、model、
  migration 或 runtime schedule。

## 9. 2026-09-30 书面澄清与 Resolution Gate

- Provider：Alpha Vantage；证据类型：直接书面支持回复，发件人为
  `AlphaVantage Support <support@alphavantage.co>`（邮箱与 ToS §19 一致）；
- 用户报告回复日期：2026-09-30；reviewed_at：2026-09-30；reviewer：Codex；
- 范围：Free API 的 `EARNINGS_CALENDAR`，仅限个人、私有、单用户、非商业使用；
- 用户原邮件逐项询问原始 CSV/JSON 长期保存、多份历史响应保留、已保存响应离线处理，
  以及派生参考字段在私有单用户应用内展示；明确排除再分发、销售、公开和多用户使用；
- Support 对四项用途均答 YES，并确认在严格个人使用条件下，没有额外保留期限、
  署名要求、缓存限制或其他条件。

回复原文的实质句：

> Yes, all the 4 uses are permitted. There are no other retention-period limits,
> attribution requirements, caching restrictions, or other conditions to follow,
> as long as you use the data strictly for personal use.

证据来自用户提供的邮件抄录，未直接读取邮箱或独立验证邮件头；原始邮件由用户
保留。本记录不保存用户个人邮箱、完整邮件头或无关邮件元数据。Support 回复仅
澄清上述当前个人使用；不解释为公开、多用户、商业、再分发、通知、终止许可后
继续使用或 canonical 使用授权。
若 API 条款、免费额度或部署/使用范围变化，必须重新执行许可 gate。

## 参考

- `docs/decisions/ADR-016-alpha-vantage-free-provider-gate.md`
- `docs/decisions/ADR-017-zero-data-cost-reference-calendar.md`
- `docs/decisions/ADR-018-reference-calendar-4.2f-a-contract.md`
- `docs/data-sources.md` §2、§8
- `docs/development-roadmap.md` Stage 4.2F-A
