# PRD-03 · get_digest（MCP 工具：一页纸现状）

## 目标

`r5Am9i` 7 个立场层工具的第 7 个：给 Agent 一个「这个事项现在进行到哪了」的轻量摘要。

## 裁决依据（已锁定）

- 返回内容 = **当前轮次 + 最新结论/决议草案 + 未决开口（卡着什么）**（owner 裁决 3，采纳推荐）
- **不含逐人立场明细**
- 命名注意：与 `hub/domain/digest.py`（内容摘要/哈希工具）区分——本工具是**事项级决策摘要**

## 做什么

1. 新增 MCP 工具 `get_digest(matter_id)`：
   - `current_round`：当前轮次号与状态
   - `decision`：最新决议（已定稿）或决议草案摘要（`pending_review` 时给草案，标注状态）
   - `open_items`：未决开口清单（来源：本轮立场的 `open_questions` 去重聚合 + 僵持/阻塞原因）
2. 出参 Pydantic 契约（`DigestOut`，`_Strict` 惯例）
3. REST 降级端点同步（D1–D6 模式）
4. 权限：复用成员闸门（非成员 404，文案同「事项不存在」）

## 边界（不做）

- ❌ 不含逐人立场、不含 `user_id`、不含 `confidence`
- ❌ 不新增 LLM 调用（摘要全部来自已落库数据；不要现场让 LLM 生成）
- ❌ 不改 `hub/domain/digest.py`

## 文件所有权

- `hub/mcp_server/methods.py`、`hub/mcp_server/tools.py`
- `hub/schemas/mcp_outputs.py`（`DigestOut`）
- `hub/web/routes_agent_rest.py`
- `tests/api/test_mcp_stance_tools.py`、`tests/api/test_rest_fallback.py`

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | 进行中事项：返回当前轮次 + 「暂无结论」占位 + 未决开口聚合 | 最小装配 |
| 2 | 有 pending_review 草案：返回草案摘要 + 状态标注 | 决议表读取 |
| 3 | 已决事项：返回定稿结论 | 终态分支 |
| 4 | 响应中**不出现** `user_id` / `confidence` / 逐人立场字段（契约钉死） | `DigestOut` 契约 |
| 5 | 非成员 → 404 不泄露存在性 | 成员闸门 |
| 6 | REST 与 MCP 逐字段一致 | D2 模式 |

## 验收标准

1. 6 个切片全绿
2. 出参字段白名单被契约钉死（手工塞一个 `user_id` 进去必须炸）
3. 全量门槛绿
4. 无任何新增 LLM 调用（grep `llm.` 于本块新增代码 = 0 命中）
