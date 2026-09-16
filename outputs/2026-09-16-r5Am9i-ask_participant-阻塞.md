# `ask_participant` 落点裁定与实施记录（`r5Am9i` 第 7 个工具 / `rpQt6D` 端点 6）

日期：2026-09-16 登记 ｜ **2026-09-17 owner 裁定选 A，已落地**

> **结论**：owner 选 **A —— 新建表 `participant_questions`**，占迁移 v9；
> 开工包 PRD-09（`rs9ncY` 决策日志）的迁移号 **顺延 v10**。
> 实现见下方「五、落地记录」。以下第一至三节保留登记时的原始事实，作为
> 「为什么不能挂到 stances.questions_for」的存档。

## 一、原始登记（2026-09-16，状态：待口径）

按开工包《全局规则 6》登记「未达成 + 原因」，不改写措辞冒充完成。

### 事实

开工包 `01-ask_participant.md` 的语义原话：

> 在目标参与人当前轮的 `questions_for` 上挂一条定向问题；若目标尚无本轮立场，
> 则登记为「待其提交立场时需回答的问题」

而实测（2026-09-16）：

1. **`questions_for` 不是「某人的收件箱」，而是 `stances` 表的一列**
   （`hub/db/models.py:263`）。`stances` 按 `(matter_id, round_number, user_id)`
   **唯一**（`:225`），即「某人某轮的那份立场」。
2. 该列在 `hub/schemas/stance.py:74` 的语义是**本人向他人提问**
   （PRD v1.2 `:599`：「`{participant_id, question}`[] ≤ 50 项，入参，向指定参与人提问」），
   不是「他人向我提问」。
3. 所以「挂到**目标**的 `questions_for` 上」，等于**拿别人的行当收件箱**：
   - 语义反了（写进去会被读成「目标在向提问者提问」）；
   - 且会**改动别人已提交的内容**，与 `stances.content_hash`（`:278`）和
     「立场是本人提交的」这条前提相冲。
4. 「若目标尚无本轮立场」这一分支更说明需要**独立于 stance 的存放处**——
   目标没交立场时，连行都不存在，无处可挂。
5. 现有表里没有这个存放处：`tasks` 无 JSON 列（`:105-115`）；`rounds.questions`
   是**全员题面**（`:100`），不是定向问题。
6. **PRD-01 的「文件所有权」清单里没有 `migrations.py`**
   （只有 `methods.py` / `tools.py` / `mcp_outputs.py` / `routes_agent_rest.py` / 测试）
   —— 说明写这份 PRD 时并未预计要新建表。

## 二、判断

这不是「实现难」，是**一处口径空白**：PRD 的语义要求一个「按 (事项, 轮次, 目标人)
存放待答问题」的实体，而数据模型里没有，PRD 也没授权新建。

## 三、两个可选落点（owner 择 A）

**A. 新建表 `participant_questions`（✅ owner 2026-09-17 选定）**
字段：`id / matter_id / round_number / target_user_id / asked_by_user_id /
question / question_hash / created_at`，唯一约束
`(matter_id, round_number, target_user_id, asked_by_user_id, question_hash)`
承担 PRD-01 片 4 的幂等。
- 优点：完全对上 PRD 语义；additive（不动任何既有表），可单独回滚；目标没交立场也能先收着。
- 代价：要占一个迁移号。**开工包 PRD-09（`rs9ncY` 决策日志）原定 v9**
  → 本项取 v9，`rs9ncY` 顺延 v10（只改一行编号，不影响其验收标准）。

**B. 不新建表，改成「投递即用」**
`ask_participant` 不落库，只把问题**带回给调用方**，由调用方在自己下一次提交里带上。
- 优点：零 schema 改动，严格落在 PRD-01 列出的文件所有权内。
- 代价：**PRD-01 片 1 的验收（「问题出现在目标 questions_for」）无法满足**，
  且「登记为待其提交立场时需回答的问题」这句被作废。等于改写 PRD 语义。

## 四、其余四片的可做性（与落点选择无关）

片 2（非成员 404）、片 3（向非参与人提问 422）、片 4（幂等）、片 5（REST 与 MCP
逐字段一致 + 通道标头）**都能直接做**，闸门与校验全部复用既有代码。

## 五、落地记录（2026-09-17，按 A 实施）

| 落点 | 文件 |
|---|---|
| 迁移 v9（DDL 冻结快照 + 可单独回滚） | `hub/db/migrations/migrations.py`、`hub/db/migrations/__init__.py` |
| 模型 | `hub/db/models.py::ParticipantQuestion` |
| 契约 | `hub/schemas/mcp_outputs.py::AskParticipantIn / AskParticipantOut / DirectedQuestionItem` |
| 唯一实现 | `hub/mcp_server/methods.py::mcp_ask_participant` |
| MCP 工具壳 | `hub/mcp_server/tools.py::ask_participant` |
| REST 端点 | `hub/web/routes_api.py` → `POST /api/items/{matter_id}/ask` |

**投递口（PRD 未写明、由本项补定）**：问题存在独立表后，通过
`mcp_get_task` 的 `directed_questions` 字段回传给**被问人自己**——
这才是「待其提交立场时需回答」的落地。该字段默认空列表，配合 `_contract`
的 `exclude_defaults`，**没有提问时该键整个不出现**，既有消费端字段集合不变。

**与 PRD-01 原文的两处差异（已登记，非擅自改写）**：
1. 落点由「目标 `questions_for`」改为独立表 —— 见第一、二节的事实。
2. 幂等由「外部幂等键」改为「正文摘要 + 四元组唯一约束」——
   `AskParticipantIn` 刻意不含 `idempotency_key`，减少一个调用方要管的字段；
   行为等价（重发返回首次那一行，不产生第二条）。

**验收对照**（PRD-01）：
1. ✅ 5 片全绿 → `tests/api/test_rpQt6D_ask_endpoint.py`（9 条：5 片 + 投递口 + 对照 + 轮次白名单 + 无配额）
2. ✅ 出参键被 `extra="forbid"` 钉死（`AskParticipantOut`）
3. ✅ 全量门槛绿
4. ✅ 无配额逻辑：`test_无配额_连问一百一十条全部成功`（旧配额是 100，越过它才有证明力）

## 六、相关

- `tests/api/test_mcp_tool_registry.py::test_ask_participant已注册为MCP工具`
  以 `xfail(strict=True)` 钉住本状态：口径定下并实现后，该用例会 XPASS →
  被报为失败，强制翻转，不会静默漏掉。
