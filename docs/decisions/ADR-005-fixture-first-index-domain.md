# ADR-005：Fixture-First Index Change Domain Development with Live Provider Licensing Gate

- 状态：已接受
- 日期：2026-07-19
- 决策者：产品负责人
- 影响阶段：3.3 指数变化与偏移识别

## 背景

Stage 3.3 需要构建 IndexChangeEvent / IndexChangeLeg 领域模型，支持 ADDED/REMOVED 原子动作、UPGRADE/DOWNGRADE/CROSS_INDEX 偏移方向分类以及 CONTINUES/ENTERS_BASE_POOL/EXITS_BASE_POOL/REENTERS_BASE_POOL 监控影响维度。

四个指数（S&P 500、Nasdaq-100、Dow 30、Russell 2000）的 live Provider 当前均未获得 GREEN status：S&P DJI 的自动化访问被安全控制拦截，Nasdaq GIW/GFID 需要商业订阅，FTSE Russell 虽然公开了 Index Notices 页面和 Attribution Requirements，但自动获取、存储和产品使用的许可尚未确认。

然而 Stage 3.3 的领域语义（ADR-002 已锁定）不依赖具体的 live Provider 实现：ADDED/REMOVED 底层事实、同日合并、1–7 日待复核、UPGRADE/DOWNGRADE/CROSS_INDEX 分类、监控影响维度都可以通过 fixture 驱动开发。

## 决策

Stage 3.3 采用 **fixture-first development**：

### 允许（In Scope）

- IndexChangeEvent 和 IndexChangeLeg 领域模型
- 原子 ADDED/REMOVED 动作的创建和持久化
- 同日自动合并、1–7 日待复核、超过 7 日独立的事件分组服务
- UPGRADE/DOWNGRADE/CROSS_INDEX/NONE 偏移方向分类
- CONTINUES/ENTERS_BASE_POOL/EXITS_BASE_POOL/REENTERS_BASE_POOL 监控影响分类
- 修正/取消的审计追溯
- 幂等重放保护
- 完整的 synthetic fixture 驱动的 targeted tests

### 禁止（Blocked by Licensing Gate）

- 真实指数 live Provider 实现
- 自动化生产环境 proprietary index data 抓取
- 商业数据 API 集成
- 完整真实 constituent dataset 的存储和公开展示

直到至少一个指数的数据访问、自动获取、存储和产品展示权利获得明确确认。

## Fixture 策略

### 优先：Synthetic Fixtures

使用完全合成的公司、证券、指数和日期构建 fixture，只测试领域行为语义。

**禁止**：将完整或大规模 proprietary constituent dataset 复制到测试、fixture、repository 或 migration 中。

### 如果使用真实历史案例

- 必须来自公开、可证实的来源（如官方公告链接）
- 必须明确记录来源 URL 和使用范围
- 只提取最小必要事实（index、security、action、dates），不保存完整原始数据
- 无法确认使用权限时，一律使用合成数据替代

## Licensing Status Language

本 ADR 中 RED / YELLOW / GREEN 评级是 **engineering admission status**——表示当前是否具备实现生产级自动化数据摄入的技术和许可条件——不是最终法律意见。

- **GREEN**：已有足够证据确认数据路径可用于计划中的 production ingestion / storage / display
- **YELLOW**：存在潜在可用路径，但自动获取、存储或产品使用权限尚未充分确认
- **RED**：当前没有已确认并可批准的 production Provider 路径

当前四个指数的 engineering admission status：

| Index | Status | Summary |
|---|---|---|
| S&P 500 | RED | S&P DJI 安全控制拦截自动化访问；商业许可协议必需 |
| Nasdaq-100 | RED | GIW/GFID 商业订阅必需 |
| Dow 30 | RED | 同 S&P 500；同一管理员（S&P DJI） |
| Russell 2000 | YELLOW | 公开 Index Notices 页面且 Attribution Requirements 明确；自动获取与产品展示许可尚未确认 |

## 结果

- Stage 3.3 domain/event model 可以基于 ADR-002 的完整语义独立开发；
- fixture-driven tests 可以覆盖全部领域规则，不依赖 live Provider；
- live Provider 的接入可以作为独立的后续 step，在 licensing gate 通过后再实现；
- 开发速度不会被 license 协商阻塞。

## 实现门

- 所有 IndexChangeLeg / IndexChangeEvent 测试必须使用 synthetic fixture（不依赖真实 proprietary data）
- 如果后续创建真实历史 fixture，必须在测试中注明来源和许可范围
- 包含新增 model 的 migration 必须随代码提交
