# PRD-04 · roR8Pk 第 1 条：立场可见性 k=3 聚合计数收口

## 目标

隐私评估证明：2–5 人事项里，逐人逐轮立场 + `user_id` 直给全体成员，可**唯一反推**谁改了立场（`tests/domain/test_stance_privacy.py` 的反推用例，当前 strict xfail）。本块把它收窄为「人数不足时只见聚合」。

## 裁决依据（已锁定）

- **k=3**：参与人数 < 3 的事项，成员只能看到**聚合计数**（各立场数量分布），看不到逐人立场明细；≥ 3 维持现状
- **三个入口统一收**：列表 / 单读 / 分析，规则**一处定义、三处复用**（owner 裁决 4）
- ⚠️ owner 未采纳「维持现状+告知」；残留风险（2 人事项只见计数）owner 已知悉接受
- 落地后**摘除反推用例的 xfail**，断言改写为「n<3 时反推不可唯一确定」

## 做什么

1. **一处定义**：新增领域函数（建议 `hub/domain/visibility.py` 或并入 `hub/api/stances.py` 私有区）：
   ```python
   def per_person_visible(*, participant_count: int) -> bool:
       return participant_count >= 3
   ```
2. **三处复用**：
   - `list_stances`（`hub/api/stances.py:315`）：n<3 时不返回逐人行，返回聚合计数载荷
   - `get_stance`（`hub/api/stances.py:287`）：n<3 时读他人立场 → 404（同「立场不存在」文案，不泄露）
   - `analyze_round`（`hub/api/stances.py:131`）：n<3 时分析出参不含可定位到人的字段（`skipped_user_ids` 等身份字段一并收掉——顺带收口附带发现 2）
3. 聚合计数载荷的 Schema（`hub/schemas/stance.py` 新增，如 `StanceAggregateRead`：`{round_number, counts: {support: n, ...}, total}`），`extra="forbid"`
4. 改造 xfail 用例：`n<3` 下断言反推集合**不可**唯一确定 → 摘 xfail 转绿

## ⚠️ 断言翻转（必须逐条登记）

| 既有断言 | 翻转 |
|---|---|
| B22 相关系列（参与人互读立场 → 200） | **仅在 n<3 场景**翻转为「只见聚合/404」；n≥3 场景**不得翻**——这是 owner 对冻结资产的显式、限定范围的修订，翻转范围必须精确到 n |
| `test_public_view_does_not_allow_identifying_stance_changer` | xfail 摘除，断言改写 |

## 边界（不做）

- ❌ 不动 Web HTML 详情页（附带发现 3 属另行登记，不在本块）
- ❌ 不动 `visibility` 字段语义（A2 已定：留痕属性）
- ❌ 不给发起人开「全知特权」之外的额外读口

## 文件所有权

- `hub/api/stances.py`（三入口接线）
- `hub/schemas/stance.py`（聚合载荷模型）
- `hub/domain/visibility.py`（如新建）
- `tests/domain/test_stance_privacy.py`（xfail 改造）
- `tests/api/test_stances_api.py`、`tests/api/test_stance_privacy_redaction.py`（翻转 + 新增）

**⚠️ 与 PRD-07（confidence 分档）同文件 → 两块串行，不同时开工。**

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | `per_person_visible` 纯函数：n=2 → False，n=3 → True | 最小函数 |
| 2 | 2 人事项 list → 只见聚合计数，响应中无 `user_id` | 列表收口 |
| 3 | 2 人事项 get 他人立场 → 404（不泄露） | 单读收口 |
| 4 | 2 人事项 analysis → 无身份字段（含 `skipped_user_ids`） | 分析收口 |
| 5 | 3 人事项三个入口行为**零变化**（既有测试不翻红） | 回归 |
| 6 | 反推用例摘 xfail 转绿 | 收尾 |

## 验收标准

1. 6 切片全绿；片 5 要求 n≥3 的既有断言**一个不动**
2. 断言翻转台账逐条登记（本块是全项目翻转最密集的块）
3. 规则只有一处定义（grep `>= 3` / `participant_count` 应只命中定义处与调用处）
4. 全量门槛绿
