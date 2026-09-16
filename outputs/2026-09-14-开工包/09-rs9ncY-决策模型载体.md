# PRD-09 · rs9ncY 决策模型载体 + 原则提炼 skill（P2）

## 目标

`rs9ncY`【P2】：决策日志载体 + 从历史决策提炼可读原则 + 产出 WorkBuddy skill。**优先级 P2，依赖前面全部跑通后再开工。**

## 事项原文要点（逐字摘录）

1. 决策日志 schema 抄 agent-decisions：`confidence / stakes / context / alternatives / reasons / outcome`；版本化抄 ADR 思路（**决策不可变、只改状态**），与 `stances.supersedes` 设计天然一致；校准借 Brier 分
2. 自研唯一一块：从历史决策提炼可读原则（LLM 离线聚类归纳）——业界无开箱实现，这是真正从零的部分
3. 产出 WorkBuddy skill：本地 WorkBuddy 能调动**留言板 + 本地决策模型 + 记忆**三者——用户最初需求的落点
4. skill 骨架用 WorkBuddy 内置 **skill-creator** 生成
5. **schema 校验要求**：决策日志条目、原则条目都要有 Pydantic 模型和校验，**禁止自由格式文本直接当数据存**

## 裁决联动

- 记忆层接入：**不做限制**（owner 裁决：各人记忆工具不同）→ skill 中记忆来源做成**可配置入口**（nmem CLI / 本地文件 / 其他），默认实现用 nmem CLI，但不绑定

## 做什么

1. **决策日志表 + Schema**：
   - 新表 `decision_logs`（走迁移机制新增 **v10**；字段按 agent-decisions 六字段 + 状态字段 + 版本字段）
   - Pydantic 模型 `DecisionLogEntry` / `PrincipleEntry`（`extra="forbid"`）
   - 版本化：决策行不可变，修订 = 新行 + 旧行状态置 superseded（同 stances.supersedes 模式）
   - Brier 分计算纯函数（校准：`predicted_confidence` vs `outcome`）
2. **原则提炼**（自研块）：离线脚本/命令，读历史决策日志 → LLM 聚类归纳 → 产出原则条目（也过 Pydantic 校验才入库）
3. **WorkBuddy skill**：用 skill-creator 生成骨架，内容 = 调动留言板 + 决策模型 + 记忆（可配置入口）

## 边界（不做）

- ❌ 不绑定具体记忆实现（裁决：可配置入口）
- ❌ 决策日志不做在线编辑（不可变，只改状态）
- ❌ 不做实时提炼（离线聚类，人工触发）

## 文件所有权

> ⚠️ **2026-09-17 编号顺延**：本 PRD 原写「走迁移机制新增 v9」，但 **v9 已被
> `r5Am9i` 的 `participant_questions`（`ask_participant` 落点）占用**
> （owner 2026-09-17 裁定）。**本项的 `decision_logs` 改用 v10**，其余不变。
> 依据：`outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md` 第五节。

- `hub/db/migrations/migrations.py`（**v10**）+ `hub/db/models.py`（新表）
- `hub/schemas/decision_log.py`（新建）
- `hub/domain/calibration.py`（Brier 分，新建纯函数）
- `scripts/distill_principles.py`（离线提炼）
- skill 骨架（WorkBuddy skills 目录，非仓库文件）
- `tests/db/test_decision_log.py`、`tests/domain/test_calibration.py`

## TDD 切片（红 → 绿）

| 片 | 红测试 | 绿实现 |
|---|---|---|
| 1 | 迁移 v10：新表就位 + 可单独回滚 | 迁移 |
| 2 | 决策日志写入 → Pydantic 校验；自由文本注入被拒 | Schema |
| 3 | 不可变性：对同一决策追加修订 → 新行 + 旧行 superseded，旧行内容不变 | 版本化 |
| 4 | Brier 分：已知输入算出已知值（手算用例） | 纯函数 |
| 5 | 提炼管线：假 LLM 输出 → 原则条目过校验入库；坏输出被拒 | 提炼管线 |
| 6 | skill 骨架存在且 frontmatter 合法（skill-creator 约定） | skill |

## 验收标准

1. 6 切片全绿
2. 决策日志/原则条目**零自由文本入库**（所有写入路径过 Pydantic）
3. v10 迁移可单独回滚（沿用 rollback_step 机制）
4. skill 能被 WorkBuddy 加载（frontmatter 校验）
5. 全量门槛绿
