# 中转站 v1 · 开工包（10 个可单独执行的 PRD）

日期：2026-09-14 ｜ 依据：`outputs/2026-09-14-裁决答复-致执行侧.md`（owner 裁决已全部锁定）
用法：**每个 `NN-*.md` 都是一个独立 PRD**，可单独认领、单独 TDD、单独验收、单独提交。
完成任意一块 → 跑门槛 → 提交（一个块一个 commit，方便回滚）。

## 全局规则（每块都适用，不在各文件重复）

1. **TDD 垂直切片**：一次一个红 → 一个绿，端到端走通再下一片。禁止横向切片（先把所有测试写完再实现）。
2. **门槛命令**（每块交付前必跑，必须亲眼看到汇总行）：
   ```bash
   DATABASE_URL=sqlite:///<临时目录>/hub.db uv run pytest tests -q
   uv run ruff check .
   ```
   ⚠️ 必须重定向 `DATABASE_URL`：`hub/main.py:144` 模块级 `app = create_app()` 会触发迁移+备份，不重定向会污染仓库根 `hub.db` 与 `backups/`。门槛后核 `hub.db` 的 sha256 不变。
3. **行尾**：本机 `core.autocrlf=true`，推送前确认 blob 无 CRLF（`git show HEAD:<path>` 检查）。
4. **新增审计常量只能用在各 PRD 里已获 owner 批准的那些**（`STANCE_EXPIRED` / `AUDIENCE_VIEW_DELIVERED` / 已有的 `SCHEMA_MIGRATED`）；**不得新增其他常量，不得动 `hub/api/errors.py` 的错误码表**。
5. **断言翻转必登记**：改动导致既有测试期望值变化时，在 commit message 或 PR 描述里逐条登记「哪条 / 为什么翻 / 新期望」。
6. **完成标准按本 PRD 的「验收标准」逐条核**，每条给 `文件:行号` 或测试名证据；做不到的登记「未达成 + 原因」，不得改写措辞冒充完成。
7. **已知陷阱**：`hub/graph/matter_graph.py` 的节点 state 返回 dict 是 LangGraph 框架要求，**不属于「禁止裸 dict」范围，不要改**。
8. **并行纪律**：一份文件同一时刻只允许一个执行者写。各块的「文件所有权」已尽量错开；撞车时按依赖序串行。

## 索引

| # | 文件 | 块 | 依赖 |
|---|---|---|---|
| 01 | `01-ask_participant.md` | MCP 工具：定向提问（无配额） | 无 |
| 02 | `02-decide_item.md` | MCP 工具：决议（**先改 PRD FR-20**） | 无 |
| 03 | `03-get_digest.md` | MCP 工具：一页纸现状 | 无 |
| 04 | `04-roR8Pk-第1条-k3聚合计数.md` | 立场可见性 k=3 收口（三入口） | 无 |
| 05 | `05-ttl过期收口.md` | ttl_seconds 过期检测 + STANCE_EXPIRED | 无 |
| 06 | `06-rsEXuh-留痕.md` | analysis 分发留痕 | 无（小） |
| 07 | `07-confidence分档外发.md` | confidence 分档 | 与 04 同文件，**串行** |
| 08 | `08-rUdiTJ-轮次编排.md` | 双环轮询 + 僵持判定 | 建议 01–03 后 |
| 09 | `09-rs9ncY-决策模型载体.md` | 决策日志 + 原则提炼 skill（P2） | 依赖前全部 |
| 10 | `10-XPIA登记.md` | XPIA 风险登记（不改扫描） | 无 |

验收门槛统一：`uv run pytest tests -q` 全绿 + `ruff` 全绿 + hub.db 零污染。
