# PRD-06 · rsEXuh ⑥留痕（AUDIENCE_VIEW_DELIVERED）

## 目标

`rsEXuh` 完成标准的「⑥留痕」缺口：`analyze_round` 的差异化分发（每个成员看到的视图）目前**没有分发留痕**。补一条审计。

## 裁决依据（已锁定）

- 审计常量 **`AUDIENCE_VIEW_DELIVERED` 已获 owner 批准**（裁决 5/§8.4-6）
- detail = `{viewer_user_id, fact_version}`（**只记这两个键**，不记视图内容）

## 做什么

1. `hub/api/audit.py` 新增一行常量：
   ```python
   # owner 已批（2026-09-14 裁决）：差异化视图每次分发写入，
   # detail = {viewer_user_id, fact_version}，不记视图内容。
   AUDIENCE_VIEW_DELIVERED = "audience_view_delivered"
   ```
2. `analyze_round`（`hub/api/stances.py:131`）在组装视图成功后写一条该审计
3. 完成标准核对：`rsEXuh` 其余子项（faithfulness 校验器、Pydantic 化）已在前序提交落地，本块只补留痕

## 边界（不做）

- ❌ 不记视图正文/节内容（审计面纪律：detail 永不含内容）
- ❌ 不改 `analyze_round` 的返回结构
- ❌ faithfulness 的「逐字等于源事实」完整校验不在本块（owner 裁决：归下轮执行侧，单独排）

## 文件所有权

- `hub/api/audit.py`（一行常量）
- `hub/api/stances.py`（一处调用）
- `tests/api/test_stance_analysis_response.py`（追加审计断言）

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | 调一次 analysis → `audit_events` 恰多一条 `audience_view_delivered`，detail 恰为 `{viewer_user_id, fact_version}` 两键 | 接线 |
| 2 | 同一成员重复调用 → 每次各写一条（分发语义是「每次交付都留痕」） | 确认无去重 |

## 验收标准

1. 2 切片全绿
2. detail 键集合被测试钉死（多一个键就红）
3. 全量门槛绿
4. `rsEXuh` 事项回填评论：⑥留痕已达成（附测试名），剩余开口 = faithfulness 完整校验归属（已裁决归下轮）
