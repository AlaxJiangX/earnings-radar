# ADR-003：财报生命周期与 Release Filing 分类

- 状态：已接受
- 日期：2026-07-13
- 决策者：产品负责人
- 影响阶段：4.x 财报与 SEC、6.x 通知

## 背景

财报新闻稿、美国发行人的 8-K、外国发行人的 6-K 以及 periodic filing 可能以不同顺序发布。表单类型本身也不能证明某份 8-K/6-K 一定包含财报发布材料，因此既不能用 EarningsEvent 的 `FILED` 状态压缩，也不能只用 form type 判定 release filing。

## 决策一：状态分离

EarningsEvent.status 只表达：

```text
SCHEDULED_ESTIMATED
SCHEDULED_CONFIRMED
RELEASED
CANCELLED
```

SEC 文件事实由 Filing 保存，FilingEarningsLink 关联 EarningsEvent。任何 Filing 都不自动推进或倒退 EarningsEvent.status。

## 决策二：Release Filing 三态分类

每条可能与财报发布相关的 Filing/FilingEarningsLink 保存：

- `release_filing_classification`：`YES` / `NO` / `REVIEW_REQUIRED`；
- `classification_reason`：可审计的分类原因或命中证据摘要；
- `classification_rule_version`：产生判断的规则版本；
- 来源证据、分类时间，以及人工复核时的操作者和复核原因。

分类原则：

- `YES`：文件内容、附件类型或明确来源证据足以证明其承载本次财报发布材料；
- `NO`：证据足以证明该文件与本次财报发布无关；
- `REVIEW_REQUIRED`：文件不可用、证据冲突、解析失败、关联事件不确定或规则无法作出可靠判断。

8-K 或 6-K 的 form type 只能形成候选，不能单独得出 YES。规则升级不得静默改写历史；需要重分类时保存旧值、新值、规则版本和原因。

## 决策三：页面派生状态

- `has_release_filing=true` 仅由与该事件关联且分类为 YES 的 Filing 推导；
- REVIEW_REQUIRED 不计为已提交，页面可显示“待复核”；
- periodic filing 由独立关系类型和实际 Filing 推导，不受 release filing 三态影响；
- 页面可以同时显示“财报已发布、8-K 已提交、10-Q 待提交”。

## 结果

- 财报发布、release filing 和 periodic filing 成为三个独立维度；
- 8-K/6-K 不会仅因表单类型被误报为财报文件；
- 人工复核与自动分类使用相同的可追溯记录；
- 通知必须基于 YES 或单独的人工确认，不能把 REVIEW_REQUIRED 当作已提交。

## 实现门

- YES/NO/REVIEW_REQUIRED 三态及原因、规则版本约束测试；
- 只有 form type、缺少内容证据时进入 REVIEW_REQUIRED 的测试；
- 人工复核和规则升级保留历史测试；
- 6-K、8-K 和多个 exhibit 的分类测试；
- release filing 与 periodic filing 独立派生测试；
- Filing 不修改 EarningsEvent.status 的测试。

## 仍待确认

- 首版分类规则允许使用的 exhibit 类型和文本证据清单；
- REVIEW_REQUIRED 在用户页面是否展示，还是只对管理员可见；
- 待复核记录的处理时限。
