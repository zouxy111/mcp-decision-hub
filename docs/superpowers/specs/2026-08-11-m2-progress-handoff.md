# M2 执行进度交接文档

- 日期：2026-08-11
- 状态：M2 进行中，任务 1–6 完成并通过双审查，任务 7 待重新分派
- 写给：接手执行的 agent（或人类）

## 1. 项目现状

仓库：`/Users/zouxingyu/Desktop/work/公司项目/mcp-decision-hub`（直接在 main 分支开发，用户已明确同意）。

- **M1 骨架：已完成**。22 个 commit，149 测试全绿，终审通过。账号/邀请/Token/事项/4 个 MCP 方法/人审三件套/幂等六象限全部落地。
- **M2 串联（DeepSeek 出题+摘要+收敛+追问）：进行中**。18 个任务的计划已定稿并**已提交 git**（commit `4f762fd`，见下方关键路径）。

### M2 已完成任务（全部通过规格+质量双审查）

| 任务 | 内容 | commit |
|---|---|---|
| 1 | 配置扩展（dotenv、4 个 LLM 字段、告知文案去"骨架阶段"） | `d0be8c0` + `d0d7fc6`（.env 入 gitignore） |
| 2 | 数据模型（round_summaries 表、Matter.granted_extra_rounds/blocked_reason） | `5444eaa` |
| 3 | domain 收敛四态（`hub/domain/convergence.py`） | `32daee0` |
| 4 | domain 收齐判定（`hub/domain/collection.py`） | `0278d53` |
| 5 | domain 轮次额度（`hub/domain/credits.py`） | `de3fb10` |
| 6 | `hub/llm/client.py` DeepSeekClient（重试 3 次退避 1/2/4s、LLMError、7 错误码、日志纪律） | `1afc34e` |

当前测试基线：**195 passed**。`uv run pytest tests -q` 可随时验证。

### 下一个要做的：M2 任务 7

**任务 7（llm/prompts.py 提示模板与注入防护，FR-18b P0）已分派但因子代理配额 429/403 失败，代码未落地。** 接手后第一件事：按计划 `docs/superpowers/plans/2026-08-11-m2-llm-pipeline.md` 行 1127-1442 重新分派实现子代理（提示词模板在 `~/.agents/skills/superpowers/subagent-driven-development/implementer-prompt.md`）。

任务 7 之后剩余：8（audit 常量+maybe_drive_round）→ 9（管线摘要阶段）→ 10（收敛分支）→ 11（reconciler）→ 12（后台接线）→ 13（start_matter 双路径首轮出题）→ 14（Web 详情页）→ 15（继续+1轮）→ 16（MCP 接真实摘要）→ 17（全量回归）→ 18（真 key 冒烟）。

## 2. 关键路径

- **PRD（产品契约，冲突时以此为准）**：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-prd.md`
- **设计文档**：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-design.md`
- **M1 计划（已执行完）**：`docs/superpowers/plans/2026-08-11-m1-skeleton.md`
- **M2 计划（执行中，已入库）**：`docs/superpowers/plans/2026-08-11-m2-llm-pipeline.md`（4719 行，18 任务，附录 A 需求映射 + 附录 B 命名总表）

## 3. 执行流程（subagent-driven-development）

每个任务严格按以下循环，**不得跳过审查**：

1. 从 M2 计划读出任务全文（`grep -n "^### 任务" docs/superpowers/plans/2026-08-11-m2-llm-pipeline.md` 拿行号），连同上下文一起发给**全新 coder 子代理**（不要让子代理自己读计划文件）
2. 实现者汇报 DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT
3. 派 explore 子代理做**规格合规审查**（不信任实现者报告，读实际代码逐行对比；DONE_WITH_CONCERNS 的偏离必须独立核实矛盾是否真实）
4. 规格通过后再做**代码质量审查**（可合并进同一个 explore 调用，顺序不能反）
5. 有问题 → 实现者修 → 重审，直至通过才进下一任务

模板：`~/.agents/skills/superpowers/subagent-driven-development/` 下 implementer-prompt.md / spec-reviewer-prompt.md / code-quality-reviewer-prompt.md。

全部任务完成后：派最终整体审查 → 收尾（main 分支无需合并）。

## 4. 已接受的计划偏离（接手者不要回退）

- 任务 1：`load_dotenv(find_dotenv(usecwd=True))`（python-dotenv 默认从调用方文件找而非 cwd）；`matter_new.html` label 去"骨架阶段"措辞
- M1 任务 14：`start_matter` 用显式 `status != "draft"` 守卫（M2 任务 13 会改回走 domain 矩阵 assert，计划已含）
- M1 任务 21：`participant_progress` 从 `round_views[0]` 取 round_id（修复计划对 ORM 对象下标的 bug）

## 5. 注意事项

- **配额**：子代理曾因引擎过载 429 / 计费配额 403 失败过。429 等 45 秒重试即可；403 是账户级限制，需用户续费或等下个账期。
- **计划文档曾丢失过一次**（未提交 git 被删），已由原作者子代理从上下文重建并提交（`4f762fd`）。重要文档务必及时 commit。
- **DeepSeek API key**：在用户机器 `~/langgraph-test/.env`（DEEPSEEK_API_KEY）。任务 18 冒烟时复制到项目根 `.env`（已 gitignore，d0d7fc6）。自动化测试全部用 FakeLLM/MockTransport，禁止真实 API 调用。
- **M2 计划已知笔误**（审查已确认不影响）：任务 6 预期"14 passed"实为 13；各处预期测试数以实际收集为准。
- **ruff format 全仓未对齐**是既有状态，lint 门只有 `ruff check`（E/F/I），不要顺手格式化全仓。
- **M3 待办**（不在 M2 范围）：决议草案/拍板/version 乐观锁/LangGraph interrupt；M4：超时调度器/换人/限流/e2e 加固。M1 终审给 M2+ 留了 8 条非阻塞建议（见 M1 终审报告，最重要的：状态推进统一走 domain 矩阵、REPLAY_EQUIVALENT 响应统一从记录取）。
- 用户偏好：全开源/本地服务，LLM 只用 DeepSeek。
