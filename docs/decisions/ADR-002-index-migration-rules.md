# ADR-002：证券级指数成员与指数偏移规则

- 状态：已接受
- 日期：2026-07-13
- 决策者：产品负责人
- 影响阶段：2.3 公司与证券、3.x 指数能力

## 背景

指数纳入对象是具体 SecurityListing/share class；同一公司又可能在相近日期发生多个指数的加入和移除。如果把证券事实、偏移方向和监控影响压成一个 change type，会丢失多 share class 事实并产生歧义。

## 决策一：成员粒度

IndexMembership 必须关联 SecurityListing：

```text
Company
└── SecurityListing
    └── IndexMembership
```

SecurityListing 表达 ticker、交易所、share class、证券类型和有效期。Company 的指数归属通过全部有效 listing 聚合；历史 ticker 和旧 membership 结束有效期但不覆盖。财报与 SEC 继续按 Company/CIK 管理。

## 决策二：底层事实与聚合窗口

IndexChangeLeg 是底层事实，`action` 只允许 `ADDED` 或 `REMOVED`，并引用具体 SecurityListing 和 IndexMembership。

对同一公司：

- 加入与移除的 `effective_date` 相同：自动合并为一个指数偏移事件；
- 生效日期相差 1–7 个自然日：不自动合并为正式偏移，生成待复核候选并保留全部 leg；
- 相差超过 7 个自然日：作为独立变化事件处理；
- 修正和取消通过事件状态、版本与审计表达，不伪装成 ADDED/REMOVED。

多 share class 同日变化时，逐证券保存 leg，再按公司和窗口聚合。

## 决策三：偏移方向

三个大型指数集合定义为 `LARGE = {S&P 500, Nasdaq 100, Dow 30}`：

- Russell 2000 移除 + LARGE 任一加入：`UPGRADE`；
- LARGE 任一移除 + Russell 2000 加入：`DOWNGRADE`；
- LARGE 内部一个或多个移除 + 另一个或多个加入：`CROSS_INDEX`；
- 不能形成上述组合的单边加入、单边移除或同指数修正：`NONE`。

只有 Russell 2000 与 LARGE 之间使用 UPGRADE/DOWNGRADE。S&P 500、Nasdaq 100、Dow 30 之间没有预设高低关系，一律为 CROSS_INDEX。

## 决策四：监控影响

监控影响独立于偏移方向：

- `CONTINUES`：变化前后公司都处于基础指数监控池；
- `ENTERS_BASE_POOL`：变化前不在基础池、历史上也从未进入，变化后进入；
- `EXITS_BASE_POOL`：变化前在基础池，变化后所有 listing 均不属于任何启用基础指数；
- `REENTERS_BASE_POOL`：变化前不在基础池，但历史上曾进入过，变化后重新进入。

用户自选股决定公司退出基础指数后是否继续被系统监控，但不改变 ENTERS/EXITS/REENTERS 对“基础指数池”的定义。

## 结果

- 同一事件可以同时为 `UPGRADE + CONTINUES`，两个维度不互相覆盖；
- 首次进入和历史重新进入不再混淆；
- 公司其他 listing 仍在指数中时，单个 share class 移除不会误判为 EXITS_BASE_POOL；
- 7 天内非同日组合必须人工复核，避免把无关调整错误合并；
- 聚合事件不能替代、删除或修改 IndexChangeLeg 原子事实。

## 实现门

- 同日自动合并、1–7 日待复核、超过 7 日分离测试；
- Russell↔LARGE 的 UPGRADE/DOWNGRADE 测试；
- LARGE 内部 CROSS_INDEX 测试；
- ENTERS 与 REENTERS 的历史判断测试；
- 多 share class 和历史 ticker 测试；
- 重跑不重复创建 leg、候选或正式聚合事件的测试。

## 仍待确认

- 待复核候选由谁处理及超时后的默认行为；
- ADR、双重上市和同 ticker 跨交易所的 SecurityListing 规范化规则；
- 各候选指数来源是否提供稳定的证券/share class 标识。
