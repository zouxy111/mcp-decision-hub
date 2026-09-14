# PRD-02 · decide_item（MCP 工具：决议，can_commit 代拍板）

## 目标

`r5Am9i` 7 个立场层工具的第 6 个：Agent 经 MCP 对事项做决议。这是**权限最重**的工具，闸门逻辑是本块的核心。

## 裁决依据（已锁定）

- **普通事项：can_commit 开通后 Agent 可代拍板终裁**（owner 裁决 2，⚠️ 未采纳「只能建草案」）
- **不可逆（irreversible）事项：强制拉本人**，Agent 无终裁权（Q9/Q11 不变）
- 闸门三项（S2）：`agent_authority` 已开 / `irreversible` 不为真 / 授权有效且**本人开给自己**
- 违规返回复用 `422 HUMAN_APPROVAL_REQUIRED`（Q12）
- 代理提交的留痕：`acting_as=agent_on_behalf` + `authority` 字段（v3 迁移已就位）

## ⚠️ 硬顺序：先改 PRD，再写代码

**本块第一步是改 `docs/superpowers/specs/2026-09-13-mcp-decision-hub-prd-v1.2.md`**：

- 修订 FR-20「Agent 无拍板权」→「Agent 默认无拍板权；`can_commit` 为显式例外（普通事项可代拍板，irreversible 事项除外）」
- 同步检查 FR-12b/12c 与第 3 章角色表的相关表述
- 该 PRD 改动**与代码改动放在同一个 commit 之前先做**（可同一 PR，但 PRD 修订必须是最先一个 commit）

## 做什么

1. 新增 MCP 工具 `decide_item`：
   - 入参：`matter_id`、`decision`（结论正文）、`rationale`、`acting_as`、`authority`、`idempotency_key`
   - `acting_as=human`：走现有决议流程（发起人本人拍板，行为不变）
   - `acting_as=agent_on_behalf`：过三道闸门，全过才放行；任一不过 → `422 HUMAN_APPROVAL_REQUIRED`
2. 闸门实现位置：服务层新增纯函数（如 `hub/domain/authority_gate.py`，零 IO、可单测），判定输入 = (matter.irreversible, participant.agent_authority, 授权记录)
3. 留痕：决议记录带 `acting_as` + `authority`；审计事件复用 `RESOLUTION_DECIDED`，detail 增加 `{"acting_as": ..., "authority": ...}`（不新增常量）
4. REST 降级端点同步（D1–D6 模式）

## 边界（不做）

- ❌ 不可逆事项的任何 Agent 终裁路径（包括「先草案后人审」的变体——irreversible 就是必须人按）
- ❌ 授权的开/管/撤界面（属 PRD 4.3 文案 + 后续 Web 工作）
- ❌ 不新增审计常量

## 文件所有权

- `docs/superpowers/specs/2026-09-13-mcp-decision-hub-prd-v1.2.md`（**先改**）
- `hub/domain/authority_gate.py`（新建，纯函数）
- `hub/mcp_server/methods.py`、`hub/mcp_server/tools.py`
- `hub/schemas/mcp_outputs.py`
- `hub/web/routes_agent_rest.py`
- `tests/domain/test_authority_gate.py`（新建）、`tests/api/test_mcp_stance_tools.py`、`tests/api/test_rest_fallback.py`

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | `authority_gate` 纯函数：irreversible=True → 一律拒（无论 authority） | 闸门函数最小实现 |
| 2 | 未开 `can_commit` 的参与人 agent 提交 → 拒 | 闸门第 1 项 |
| 3 | 授权非本人开给自己 → 拒 | 闸门第 3 项 |
| 4 | MCP 端到端：三闸门全过的代理提交 → 决议落定，留痕 `acting_as`/`authority` | 接线 |
| 5 | 闸门任一不过 → 422 `HUMAN_APPROVAL_REQUIRED`，错误形状与 MCP/REST 一致 | 错误接线 |
| 6 | `acting_as=human` 发起人拍板行为**零变化**（既有测试不翻红） | 回归验证 |

## 验收标准

1. PRD FR-20 已修订且与代码行为一致（引用 PRD 节号 + 代码 `文件:行号`）
2. 6 个切片全绿；片 6 特别要求：**不动既有测试**前提下全绿（动了就要登记断言翻转并说明）
3. 纯函数闸门有独立单测，不依赖 DB
4. 全量门槛绿
5. 断言翻转台账：本块若翻转任何既有断言，逐条登记
