# PRD-08 · rUdiTJ 轮次编排：双环轮询 + 僵持判定

## 目标

`rUdiTJ`【P1】：外层轮次编排 + 内层拉取节奏。**验收只有一条：一条测试证明「无新增信息时不进下一轮、直接判僵持」。**（事项原文验收，逐字）

## 事项原文要点（逐字摘录，完成标准以此为准）

- 收敛判据已按审计改写：**主指标改桥接共识（跨阵营共同点）**——只有它能挡住「代理互相讨好导致的假收敛」；CD 仅作辅助；**最保守选项：先不换指标，继续用现有 LLM 四态**
- 外层：不收敛 → 判「有新增信息吗」：**没有则判僵持、立即拉人，不空转**；有则只回传差异点进下一轮
- 轮次上限 → 强制拉人，**接现有换人流程 `hub/api/reassignment.py`（FR-16），不要另起一套**
- 内层：基础间隔 + 指数退避；空闲降频；urgency=high / deadline 临近提频；活跃轮次提频、等人工降频。**复用现有退避（`hub/domain/retry.py`），不要重写**
- 硬规则：并行表态不串行；每轮必须带新信息；不重推全部立场；超时记 missing 不默认同意（仓库已解决，FR-14，只需确认沿用）
- 授权边界：V1 锁死 propose_only

## 裁决联动

- 裁决 2 说明：`rUdiTJ` 自述「V1 锁死 propose_only」是**本事项自身范围**口径，与 decide_item（PRD-02）的 can_commit 裁决不冲突——本块的编排层不实现任何拍板能力。

## 现状锚点（已核实）

- 现有收敛：`hub/domain/convergence.py` LLM 四态字符串校验（continue / provisionally_ready / converged / blocked），`RoundSummary.convergence` 落库（`hub/db/models.py:144`）
- 现有编排图：`hub/graph/matter_graph.py`——branch 路由在 `_compute_branch_route`（`:96-125`）；**换指标会动这里与一批测试，成本不低**（事项原文已自知）
- 额度授予：`hub/domain/credits.py` `can_auto_advance` + `CREDIT_GRANT_PER_CONTINUE`
- 超时：PRD FR-14，`hub/api/scheduler.py` 记 timeout 不伪造（本项**只确认沿用**）

## 做什么（按「最保守选项」为默认路径）

1. **僵持判定**（核心新增）：不收敛时先算「本轮相对上轮有无新增信息」：
   - 新增信息定义：本轮立场集合相对上轮有**任何内容差异**（新立场 / 立场变更 / 新增 open_questions / 新增 questions_for）
   - 纯函数实现（建议 `hub/domain/stall_detect.py`），输入两轮立场摘要，零 IO
2. 无新增信息 → 判僵持：**立即拉人**（接 `hub/api/reassignment.py` 现有流程），**不开下一轮**
3. 轮次上限到 → 强制拉人（同一流程，不另起）
4. 内层拉取节奏：提频/降频规则落地到轮询提示（`next_poll_after` 已有机制，`hub/mcp_server/methods.py:103-104` 的 poll_seconds 切换是现成接缝）
5. 桥接共识主指标：本块**只做接缝**（收敛判定函数抽象成可替换策略），**不换指标**（最保守选项；换指标的成本事项原文已预警）

## 边界（不做）

- ❌ 不换收敛主指标（桥接共识只留策略接缝）
- ❌ 不重写退避（复用 `hub/domain/retry.py`）
- ❌ 不另起换人流程（接 reassignment.py）
- ❌ 不动 `matter_graph.py` 节点 state 的 dict 返回（框架要求）
- ❌ 不做任何拍板能力（V1 propose_only）

## 文件所有权

- `hub/domain/stall_detect.py`（新建，纯函数）
- `hub/graph/matter_graph.py`（branch 路由加僵持分支）
- `hub/api/pipeline.py`（`_branch_phase` 接僵持判定）
- `hub/api/reassignment.py`（**只读复用，不改**）
- `hub/domain/convergence.py`（策略接缝，小改）
- `tests/domain/test_stall_detect.py`（新建）、`tests/integration/`（僵持 e2e）

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | ⭐ **验收切片**：两轮立场内容完全一致 → 判僵持、调拉人流程、**不产生新轮次** | 僵持判定 + 接线 |
| 2 | 有新增信息（任一差异）→ 正常开下一轮，只回传差异点 | 差异计算 |
| 3 | 达轮次上限 → 强制拉人（复用现有流程，不重造） | 上限分支 |
| 4 | 拉取节奏：urgency=high / 活跃 / 空闲 / 等人工 四种状态的 poll 间隔单调性 | 节奏规则 |
| 5 | 既有轮次流程回归：正常收敛路径零变化 | 回归 |

## 验收标准

1. **片 1 即事项验收标准本身**，必须真红后转绿
2. 拉人走的是 `hub/api/reassignment.py`（grep 证明无第二套换人逻辑）
3. 退避复用 `retry.py`（grep 证明无新退避实现）
4. 全量门槛绿
5. 开口登记：「桥接共识换主指标」未做（最保守选项 + 接缝已留）
