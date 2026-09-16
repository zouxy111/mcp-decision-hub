# PRD-01 · ask_participant（MCP 工具：定向提问）

> ✅ **2026-09-17 已落地**。落点与本文原文有一处差异：问题**不写进**
> `stances.questions_for`（那是「本人向他人提问」的列，且目标未提交立场时该行
> 不存在），改为独立表 `participant_questions`（迁移 v9），并经
> `get_task.directed_questions` 投递给被问人。幂等改由「正文摘要 + 四元组唯一
> 约束」承担，入参不含外部幂等键。详见
> `outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md`（owner 2026-09-17 裁定选 A）。

## 目标

`r5Am9i` 7 个立场层工具的第 5 个：让 Agent 能就某事项向某位参与人发起**定向追问**。

## 裁决依据（已锁定，不再讨论）

- **无配额**（owner 裁决 1：先不限制）。roR8Pk 第 4 条以「owner 决定不做」关闭。**不要实现任何计数 / 滑窗 / 429 逻辑。**

## 做什么

1. 新增 MCP 工具 `ask_participant`：
   - 入参：`matter_id`、`target_user_id`、`question`（正文）、`round_number`（可选，缺省=当前最新轮）
   - 语义：在目标参与人当前轮的 `questions_for` 上挂一条定向问题；若目标尚无本轮立场，则登记为「待其提交立场时需回答的问题」
2. 出参加 Pydantic 契约（`hub/schemas/mcp_outputs.py`，沿用 `_Strict` / `extra="forbid"` 惯例）
3. REST 降级端点同步落地（D1–D6 已确立的模式：与 MCP 同一 method、同一模型、同一字段集合，`X-Hub-Channel: rest` 头）

## 边界（不做）

- ❌ 不做配额、限流、429
- ❌ 不做「追问后强制对方重答」的状态机（只挂问题，回应由对方下轮立场承载）
- ❌ 不动 `hub/domain/rate_limit.py`

## 文件所有权

- `hub/mcp_server/methods.py`（新增 `mcp_ask_participant`）
- `hub/mcp_server/tools.py`（注册工具壳）
- `hub/schemas/mcp_outputs.py`（In/Out 模型）
- `hub/web/routes_agent_rest.py`（REST 端点）
- `tests/api/test_mcp_stance_tools.py`、`tests/api/test_rest_fallback.py`（追加用例）

## TDD 切片（红 → 绿）

| 片 | 红测试（先写，必须真红） | 绿实现 |
|---|---|---|
| 1 | 成员向参与人提问 → 201/成功，问题出现在目标 `questions_for` | 最小落库 |
| 2 | 非成员提问 → 404，且文案与「事项不存在」一致（不泄露事项存在性） | 复用 `_require_matter_access` |
| 3 | 向非参与人提问 → 422 VALIDATION_FAILED | 参与人校验 |
| 4 | 重复提交同一问题（幂等键相同）→ 返回首次结果，不产生第二条 | 复用 `decide_idempotency` |
| 5 | REST 端点与 MCP 返回**逐字段相同**，带 `X-Hub-Channel: rest` | D2/D4 既有模式 |

## 验收标准

1. 5 个切片测试全绿，且片 1 在实现前确实是红的
2. 出参键被 `extra="forbid"` 契约钉死（多一个键就 500/ValidationError）
3. 全量门槛绿（见 README 全局规则 2）
4. 无配额逻辑：代码中**不存在**针对 ask 的计数/限流（grep 可证）
