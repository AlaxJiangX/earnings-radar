# Earnings Radar 数据源规划

> 状态：规划稿。本文定义 Provider、许可、来源追溯和新鲜度验收方式；不代表任何供应商已签约或已接入。

## 1. 范围与原则

MVP 只接入支撑以下能力的数据：公司/CIK/证券身份、四个基础指数成分与公告、预计财报日历、有限公司 IR 官方确认，以及 SEC 文件。市场行情与 Reddit 属于第二阶段，不进入本文件的 MVP 接入计划。

所有外部数据必须：

- 通过 Provider 适配器获取，View 不直接请求外部源；
- 在标准化前保存 RawDataRecord、来源 URL、抓取时间和内容哈希；
- 记录 Provider、解析器版本、SyncRun、原始值和标准化值；
- 有明确超时、限速、User-Agent、重试和错误分类；
- 经过许可、再展示权和保留政策检查后才能进入生产；
- 支持固定 fixture 的契约测试，普通 CI 不访问真实网络；
- 在来源冲突时保留全部证据，不静默覆盖；
- 以稳定业务键和数据库约束保证幂等。

## 2. MVP 数据源清单

| 能力 | 首选来源类型 | 关键输入 | 标准化输出 | 选择状态 |
|---|---|---|---|---|
| 公司、CIK | SEC 官方数据 | CIK、发行人名称、ticker 映射 | Company、SecurityListing 识别证据 | SEC 为官方基线；具体 endpoint 待确认 |
| SEC 文件 | SEC EDGAR | accession number、form、accepted_at、period、documents | Filing、FilingDocument、FilingEarningsLink 候选 | 官方来源；访问策略待实现前核对 |
| 财报日历 | 合法第三方 API | 预计日期、时段、财年/期间、供应商事件 ID | EarningsEvent 候选/预计安排 | **供应商与许可待产品确认** |
| IR 官方确认 | 公司 IR 页面或有限 IR Provider | 正式日期、电话会、新闻稿链接 | 确认状态、发布日期、来源证据 | 首批公司清单与抓取方式待确认 |
| S&P 500 | 官方公告、合法 API 或受控导入 | 证券/ticker、公告日、生效日、成分快照 | SecurityListing 级 IndexMembership、IndexChangeLeg | **来源与许可待产品确认** |
| Nasdaq 100 | 官方公告、合法 API 或受控导入 | 同上 | 同上 | **来源与许可待产品确认** |
| Dow 30 | 官方公告、合法 API 或受控导入 | 同上 | 同上 | **来源与许可待产品确认** |
| Russell 2000 | 官方公告、合法 API 或受控导入 | 同上 | 同上 | **来源与许可待产品确认** |

不得把网页可访问等同于允许自动抓取、长期保存或公开再展示。每个指数与财报供应商必须单独记录许可结论。

### 2.1 候选入口（仅供人工评估）

下列项目只证明存在潜在数据入口，不代表 Earnings Radar 获得了抓取、缓存、衍生、再分发或商业使用许可：

| 能力 | 候选项 | 当前结论 |
|---|---|---|
| SEC/CIK/Filings | [SEC EDGAR API 概览](https://www.sec.gov/files/edgar/filer-information/api-overview.pdf)、SEC 官方 submissions/filing 数据 | 官方候选；仍需人工核对访问政策、User-Agent、限速、缓存和再展示要求 |
| S&P 500 / Dow 30 | [S&P DJI Index Announcements](https://www.spglobal.com/spdji/en/index-announcements/)、获许可供应商、受控人工导入 | 候选；成分明细、历史、自动化访问与公开再展示许可均未确认 |
| Nasdaq 100 | Nasdaq Global Index Watch/官方公告、获许可供应商、受控人工导入 | 候选；GIW/API 权限、下载自动化、缓存和再展示许可均未确认 |
| Russell 2000 | [FTSE Russell Index Notices](https://www.lseg.com/en/ftse-russell/index-resources/notices)、[Russell 2000 页面](https://www.lseg.com/en/ftse-russell/indices/russell-2000-index)、获许可供应商、受控人工导入 | 候选；部分完整公告可能需要订阅，成分数据使用与再分发许可未确认 |
| 财报日历 | [Nasdaq Earnings Calendar](https://www.nasdaq.com/market-activity/earnings)、[Finnhub Earnings Calendar API](https://finnhub.io/docs/api/introduction)、[Alpha Vantage Earnings Calendar](https://www.alphavantage.co/documentation/)、[FMP Earnings Calendar](https://site.financialmodelingprep.com/developer/docs/stable) | 功能候选；任何免费层、网页或 API 的生产使用、缓存、历史保留、公开展示和开源自托管授权均未确认 |

### 2.2 每个候选必须人工验证的许可问题

- 是否允许服务器端自动访问，以及必须使用的认证、User-Agent、速率和并发限制；
- 是否允许缓存原始响应，允许保留多久，终止订阅后是否必须删除；
- 是否允许保存标准化/衍生数据和历史变更；
- 是否允许在公开网站展示公司、ticker、日期、指数成员与变动；
- 是否允许在 GitHub 开源、自托管实例或多个部署环境中使用；
- 是否限制商业使用、用户数量、地域、调用量或批量下载；
- 是否要求署名、链接回源、版权声明或显示延迟标签；
- 是否允许把数据用于邮件/站内通知；
- 是否允许通过 API、导出或数据库备份间接再分发；
- 供应商 schema、字段定义和服务终止后是否有迁移/删除义务。

人工审查结果必须记录审查人、日期、条款版本、证据链接和结论。结论未记录前，只能使用人工编写 fixture，不得接入真实 Provider。

## 3. Provider 契约

每个 Provider 必须声明：

- `provider_key`、版本、DataSource 和支持的能力；
- 请求范围、分页/游标和稳定请求指纹；
- 连接与读取超时；
- 上游限速、并发限制、重试上限和退避策略；
- 身份标识要求，例如 CIK、exchange+ticker、供应商事件 ID；
- 原始响应类型及敏感字段过滤规则；
- normalizer/parser 版本；
- 临时错误、永久错误、限速错误和数据校验错误；
- 空响应、异常缩减和 schema 变化的保护策略；
- 可用于 smoke test 的最小安全范围。

Provider 只返回原始数据引用与标准化 DTO，不直接写 Company、SecurityListing、IndexMembership、EarningsEvent 或 Filing。领域服务负责核对、事务、变更历史和通知。

### 3.1 原始响应保存基线

- `content_hash` 使用原始 bytes 的 SHA-256；`request_fingerprint` 使用规范化方法、URL 和不含凭据值的请求身份生成；
- `source + request_fingerprint + content_hash` 唯一，防止同一请求正文被重复保存；
- 每个看到该正文的 SyncRun 通过唯一的 RawDataObservation 留痕，幂等重跑不重复 observation；
- 初始数据库硬限制单条 payload 不超过 1 MiB，环境配置只能设置更低上限，超限内容在写库前拒绝；
- 1 MiB 只是 alpha 前的工程保护值。供应商许可、原始响应保留期限、超限响应处理和长期存储方案仍需产品与运维确认。

## 4. 身份映射

### 4.1 公司与证券

- CIK 优先识别 Company，并以保留前导零的字符串保存；
- ticker 必须与交易所、share class 和有效期一起映射到 SecurityListing；
- Provider 的指数成分必须先匹配具体 SecurityListing，才能创建 IndexMembership；
- 无法确定 share class 或交易所时进入待复核，不得退化为 Company 级 membership；
- ticker 变更创建/结束 listing 有效期，不改写历史 membership；
- ADR、多上市或同 ticker 跨交易所冲突必须保留来源并等待身份规则确认。

### 4.2 财报事件

- 正式身份由 `company_id + period_end_date + period_type` 生成；
- Provider 外部事件 ID 只用于未知 period_end_date 的候选去重和来源追踪；
- 候选事件不得仅凭 fiscal_year/fiscal_period 变为正式事件；
- 候选提升、合并或拆分必须写 DataChange 和 identity rule version；
- 具体规则见 ADR-001。

### 4.3 SEC 文件

- Filing 按规范化 accession number 全局去重；
- CIK 用于公司确定性匹配；
- FilingEarningsLink 按报告期、表单类型、时间窗口和规则版本匹配；
- `RELEASE_FILING` 与 `PERIODIC_FILING` 独立，不改变 EarningsEvent.status。

## 5. 来源优先级与冲突

默认原则是直接官方证据优先于第三方预计数据，但具体字段级矩阵仍需产品确认：

| 字段类别 | 默认高优先来源 | 低优先来源用途 |
|---|---|---|
| SEC accepted_at、form、accession | SEC 官方 | 不允许第三方覆盖 |
| 财报正式日期、电话会 | 公司 IR 官方 | 第三方可补充候选，不可无审计覆盖 |
| 预计财报日期 | 财报日历 Provider | IR 发布后转为官方确认维度 |
| 指数公告日/生效日 | 指数官方公告或获许可权威源 | 快照用于交叉验证 |
| ticker/CIK | SEC 与交易所/权威标识源 | 其他源只提供匹配线索 |

发生冲突时应保存所有 SourceEvidence、当前选中证据、选择规则版本和冲突状态。管理员修正必须写原因；是否锁定字段、防止后续自动覆盖仍待确认。

SourceEvidence 不直接依赖领域 app：目标使用受限 `target_type` 和 UUID，目标是否存在由后续领域 service 校验。其 evidence_key 由 RawDataRecord、目标、字段、规范化 JSON 值和 normalizer version 生成；同一 RawDataRecord 的相同标准化事实重跑时复用现有证据，不同 RawDataRecord 则分别留证。SyncRun 不进入 evidence_key，但证据写入必须引用该 SyncRun 对 RawDataRecord 的 RawDataObservation，并拒绝包含 API key、Authorization、密码、session 或 Token 的 JSON。

## 6. 数据新鲜度目标

以下数值已确认为首版内部工程目标，不是对外 SLA。测量口径、alpha/beta 达标率和无样本处理见 ADR-004。

| 数据/动作 | 初始目标 | 测量方式 | 状态 |
|---|---:|---|---|
| SEC 文件发现 | 15 分钟内 | `Filing.created_at - Filing.accepted_at`，另标记上游时间异常 | 已确认初始目标 |
| 财报日历更新 | 上游变化后 24 小时内 | `normalized_at - source_observed_at`；无上游变更时间时用相邻快照估算 | 已确认初始目标 |
| IR 确认更新 | 官方发布后 6 小时内 | `normalized_at - official_published_at`；缺失时使用首次观察时间 | 已确认初始目标 |
| 指数公告更新 | 官方公告后 24 小时内 | `IndexChangeEvent.created_at - announcement_published_at` | 已确认初始目标 |
| P0/P1 通知发送 | 事件入库后 5 分钟内 | `provider_accepted_at/in_app_created_at - domain_change_committed_at` | 已确认初始目标 |

每个 SyncRun 汇总目标内/超目标数量和最大延迟。健康检查报告最近成功运行与数据年龄，但不把上游未发布新数据误判为本系统陈旧。

## 7. 调度建议

| Provider/任务 | PRD 频率 | 初始计划 | 备注 |
|---|---:|---:|---|
| SEC | 5–15 分钟 | 先按 10 分钟预算验证 | 必须服从 SEC 访问政策；若政策/平台不允许则回到产品评审 |
| 财报日历 | 每天 | 每日一次 | 如供应商支持变更流，再单独评估 |
| IR | 每 6 小时或每天多次 | 每 6 小时 | 仅首批重点公司 |
| 指数 | 每天 | 每日快照 + 公告检查 | 来源能力未确认 |
| 通知 | 每 5 分钟 | 每 5 分钟 | 需验证托管平台 Cron 能力 |

调度频率不等于新鲜度保证：上游发布时间、速率限制、任务积压和解析失败必须分别计量。

## 8. 许可与上线门

每个真实 Provider 上线前必须记录：

- 服务条款和许可链接、审查日期、负责人；
- 是否允许自动访问、缓存原始响应、长期保留和公开再展示；
- 署名、链接回源和删除义务；
- API key 的存储、轮换和最小权限；
- 免费/付费配额、超额行为和成本上限；
- 数据地域、个人数据和安全要求；
- 终止服务后的数据删除或替换方案。

任一项未知时只能使用 fixture 或受控开发 smoke test，不能进入生产同步。

## 9. 失败保护与验收

- 指数快照为空或成分数量异常下降时，停止差异落库，不批量生成 REMOVED；
- 财报日历 schema 变化时保留 RawDataRecord 并将运行标为 partial/failed；
- SEC/IR 临时失败只按有限次数重试，不删除既有记录；
- Provider 时间戳必须带精度与时区；不能解析时保留原值并待复核；
- 同一 fixture 连续处理两次，第二次不得新增领域记录、变化或重复原始正文；
- Provider 契约、身份匹配、来源冲突和新鲜度计算必须有自动测试。

## 10. 仍待确认

- 财报日历供应商、四个指数来源和各自许可；
- 首批 IR 公司清单与允许的抓取方式；
- 字段级来源优先级和管理员修正锁定机制；
- Provider 限速、失败重试和成本阈值；
- 原始响应保留期限、长期容量/删除政策，以及 1 MiB 初始保护值是否需要按已许可数据源调整；
- 生产平台是否能支持计划频率及最长运行时间。
