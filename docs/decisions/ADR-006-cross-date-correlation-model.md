# ADR-006：跨日期指数变化关联与候选持久化模型

- 状态：已接受
- 日期：2026-07-19
- 决策者：产品负责人
- 影响阶段：3.3 指数变化与偏移识别

## 背景

ADR-002 明确：

- 同一 effective_date 的 ADDED/REMOVED legs → 自动合并为一个 IndexChangeEvent
- effective_date 相差 1–7 天 → 生成待复核候选，保留全部 legs
- 相差超过 7 天 → 独立事件

当前 Stage 3.3 Step 2 已经实现了 same-date canonical event：

`(company, effective_date, status=ACTIVE)`

剩余职责是处理 cross-date event 之间的 candidate correlation。

## 决策一：Candidate Persistence — Separate `IndexChangeCorrelation` Model

选择独立 model 而非 Event field 或 derived query。

理由：

- Event field 方案无法表达 "event A 与 event B 相关" 的对称关系——必须选择其中一个 event 持有 FK
- Derived query 方案无法保存 review 状态：PENDING → CONFIRMED / REJECTED 需要持久化
- ADR-002 的 "待复核候选" 明确是业务状态，需要 review workflow
- 独立 model 允许在不修改 underlying events/legs 的前提下管理 correlation lifecycle

### Model 设计

```
IndexChangeCorrelation
├── earlier_event  FK(IndexChangeEvent, PROTECT)
├── later_event    FK(IndexChangeEvent, PROTECT)
├── status         PENDING | CONFIRMED | REJECTED
├── displacement   UPGRADE | DOWNGRADE | CROSS_INDEX | NONE
├── monitoring_impact    CONTINUES | ENTERS_BASE_POOL | EXITS_BASE_POOL | REENTERS_BASE_POOL
├── created_at
└── updated_at
```

## 决策二：Pairwise 结构

选择 pairwise（event A ↔ event B），不是 group。

理由：

- ADR-002 语义是 "同一公司 1–7 天内的 events" 之间产生候选，没有 "多个 events 形成候选组" 的概念
- Pairwise 更简单：canonical key 是 `(earlier_event, later_event)`
- 如果同一公司 7 天内有 A(June 20), B(June 23), C(June 26)，自然形成 A-B, B-C, A-C 三个 pairwise candidates
- 不需要引入 group 的额外抽象层

## 决策三：Canonical Identity

`UNIQUE(earlier_event, later_event)`

DB constraints:

- CheckConstraint: earlier_event != later_event
- CheckConstraint: both events belong to same company (via service validation，不在 DB constraint)
- UNIQUE on (earlier_event, later_event)
- Service-level: gap must be 1–7 days

## 决策四：Candidate Lifecycle

```
PENDING → CONFIRMED
PENDING → REJECTED
```

- 创建时默认 PENDING
- Status 可原地修改（同一 record 上 PENDING → CONFIRMED / REJECTED）——这不是 audit record，而是 review state
- CONFIRMED 和 REJECTED 都永久保留
- Underlying Events / Legs 始终不修改、不删除

## 决策五：Classification Ownership

### Same-date (已由 Step 2 Event 支持)

```
Event.displacement
Event.monitoring_impact
```

Same-date Event 内 legs 的分类直接写在 Event 上。

### Cross-date (本 ADR 新增)

```
Correlation.displacement
Correlation.monitoring_impact
```

Cross-date 分类写在 Correlation 上，不写在单个 Event 上。避免 "UPGRADE 到底属于 Event A 还是 Event B" 的歧义——它属于 their relationship。

## 决策六：Monitoring Impact

Monitoring impact 持久化位置与 displacement 一致：

- Same-date: Event.monitoring_impact
- Cross-date: Correlation.monitoring_impact

Monitoring impact 独立于 displacement 维度（ADR-002）：
同一 event/correlation 可以同时是 `UPGRADE + CONTINUES`，或 `CROSS_INDEX + ENTERS_BASE_POOL`。

## 决策七：Candidate Generation Boundary

Step 3 service 职责：

```text
输入: 新创建的 ACTIVE Event (刚刚由 record_index_change_leg 创建)
      ↓
查找: same company, different effective_date, 1-7 day gap
      ↓
为每个符合条件的 existing event 创建 PENDING candidate
      ↓
Candidate 幂等: 如果 (earlier_event, later_event) 已存在，不创建 duplicate
```

不进行批量 scan——每次只对新 event 生成 candidate。

Same-date Events 不进入此流程（已由 canonical event 处理）。

## 决策八：Correction / Cancellation 兼容性

如果 Event A 被 CORRECTED（创建新的 supereding Event A'）：

- 旧的 `(A, B)` candidate 保持原样（历史 provenance 不可删除）
- 系统可以选择：自动为 `(A', B)` 创建新 candidate，或标记旧 candidate 为 stale
- 本 ADR 不强制自动行为——留到 correction service 实现时决定

FK 使用 PROTECT 而非 CASCADE 或 SET_NULL，确保 correlation 对应的 events 不能被误删除。

## 实现门

- IndexChangeCorrelation model + migration
- Same-date 分类：displacement + monitoring_impact 写 Event
- Cross-date candidate generation + PENDING creation
- Cross-date confirm 时写 Correlation.displacement / monitoring_impact
- Pairwise canonical uniqueness via DB constraint
- Idempotent candidate generation
- Synthetic fixture tests 覆盖全部场景
