# MCP 决策中台 · M4 加固完成报告

- 日期：2026-08-13
- 仓库（多端同步，同一仓库）：
  - `/Volumes/ZXSSD/work/公司项目/mcp-decision-hub`（原开发机）
  - `/Users/zouxingyu/Desktop/work/公司项目/mcp-decision-hub`（本机，main 分支直接开发）
- 当前状态：**M4 加固全部完成**，454 个自动化测试全绿，ruff check 零错误

---

## 1. 一句话总览

在 M3 决议闭环之上完成了最后的 P0 缺口与加固：超时调度器（FR-14b）后台周期扫描到期任务并推进收齐/汇总/阻塞，换人服务（FR-08b）支持发起人在 collecting/blocked 状态下替换参与人，MCP 限流（PRD 9.1）按 Token/账号双维度真实计数并返回429+Retry-After，注入防护闭合（FR-18b）修复摘要/追问 prompt 的平铺窗口并转义数据段闭合标记，tick/resume 同 matter 串行化消除了双 worker 并发窗口，运维可观测页（/admin/ops）与备份脚本/部署文档落地。

## 2. 里程碑交付情况

| 里程碑 | 内容 | 状态 | 测试基线 | 关键证据 |
|---|---|---|---|---|
| M1 骨架 | 账号/邀请/Token/事项/4 个 MCP 方法/人审三件套/幂等六象限 | ✅ 完成 | 149 passed | M1 终审通过 |
| M2 串联 | DeepSeek 出题/摘要/四态收敛/多轮追问/后台管线/重启恢复 | ✅ 完成 | 257 passed | 真实 DeepSeek 两轮闭环冒烟通过 |
| M3 闸门 | LangGraph 编排/决议草案/拍板/version 乐观锁/Web 闭环/审计查询 | ✅ 完成 | 380 passed | 真实 DeepSeek 决议闭环冒烟通过 |
| **M4 加固** | 超时调度器/换人/限流/注入闭合/串行化/运维交付 | **✅ 完成** | **454 passed** | 见下文明细 |

当前 HEAD：`56c16ee`（74 commits 总数）。代码规模：hub 约 5500 行，tests 约 8500 行，64 个测试文件。

## 3. M4 交付明细

### 3.1 功能落地

- **FR-14b 超时调度器**：`hub/background.py` 新增 `timeout_worker` 协程（先扫后睡，启动即扫一次）；`hub/api/scheduler.py` 实现 `scan_once`（条件 UPDATE `WHERE status='pending' AND deadline_at<=now` + `task_timeout` 审计 + `maybe_drive_round` 收齐判定）；幂等（重复扫描 no-op）；重启自动重建（lifespan 无条件创建）；运行状态单例 `SCHEDULER_STATE` 供 `/admin/ops` 展示。可配置 `TIMEOUT_SCAN_INTERVAL_SECONDS`（默认 60s）。
- **FR-08b 换人**：`hub/api/reassignment.py::reassign_task`——仅发起人、仅 collecting/blocked、仅 pending/timeout 任务；原任务条件 UPDATE `reassigned`（rowcount 校验）；新任务同轮同题新截止（`timeout_seconds` 重算）；`MatterParticipant` 追加新参与人（不移除原参与人行）；`blocked` 时条件 UPDATE `blocked→collecting`（`blocked_reason` 清空）；`task_reassigned` 审计（五键 detail：原任务/新任务/轮次/from/to）；Web 端 `/matters/{id}/reassign` 路由 + 详情页换人表单。
- **MCP 限流（PRD 9.1）**：`hub/domain/rate_limit.py::RateLimiter`（滑动窗口，`threading.Lock`）；`hub/mcp_server/auth.py` 鉴权后检查 token 维度（默认 60/min）+ 账号维度（默认 120/min），拒绝返回 HTTP 429 + `Retry-After` 头；`hub/mcp_server/tools.py` 的 `submit_output` 工具内检查 submit 维度（默认 10/min，`ToolError` 携带 `RATE_LIMITED` 错误码）。`next_poll_after` 改读 `settings.poll_seconds_idle/active`（可配置）。
- **注入防护闭合（FR-18b）**：`hub/llm/prompts.py` 的 `wrap_user_content` 增加 `html.escape(text, quote=False)` 转义——数据段内的 `<`/`>` 被转义为 `&lt;`/`&gt;`，防止 `</user_submitted_content>` 闭合标记伪造；`build_round_summary_prompt` 的 previous_summary 条目与 `build_followup_questions_prompt` 的摘要条目改为逐条 `wrap_user_content`（修复平铺窗口，与 M3 决议草案 prompt 对齐）。
- **tick/resume 串行化**：`hub/background.py` 新增 `_matter_locks` 字典 + `_matter_lock(matter_id)`；`drive_worker` 先解析 round→matter（`pipeline.resolve_round_matter`）再 `async with _matter_lock(matter_id)` 持锁跑管线；`resume_worker` 同样按 matter 持锁。不同 matter 互不阻塞。
- **运维可观测**：`/admin/ops` 页面展示 `SCHEDULER_STATE`（五字段）、`drive_queue`/`resume_queue` 队列深度、最近 `task_timeout`/`task_reassigned` 审计各 25 条。
- **备份与部署**：`scripts/backup_db.py` 使用 `sqlite3.Connection.backup()` API（WAL 安全、不停服）；`docs/ops/deployment.md` 覆盖环境要求、`.env` 全量变量表（含 M4 新增 13 项）、启动命令、NTP 要求、备份/恢复 runbook、健康检查、已知限制。
- **配置扩展**：`Settings` 新增 6 字段（`timeout_scan_interval_seconds`、`rate_limit_*` 三项、`poll_seconds_*` 两项）；`load_settings()` 补齐全部 13 项 env 读取（修补 M2/M3 遗留）。
- **审计新常量**：`TASK_TIMEOUT`、`TASK_REASSIGNED`。FR-23 审计覆盖"超时判定、换人"至此闭合（发布门槛：关键动作审计覆盖率 100%）。

### 3.2 工程质量

- **测试**：454 passed / 0 failed（`uv run pytest tests -q`）；ruff check（E/F/I）零错误。
- **新增测试文件**：`tests/api/test_scheduler.py`（8）、`tests/api/test_reassignment.py`（18）、`tests/api/test_worker_serialization.py`（6）、`tests/domain/test_rate_limit.py`（8）、`tests/llm/test_prompt_injection_hardening.py`（11）、`tests/integration/test_mcp_rate_limit.py`（10）、`tests/integration/test_m4_acceptance.py`（5）、`tests/web/test_reassignment_web.py`（5）、`tests/web/test_admin_ops.py`（2）、`tests/ops/test_backup_script.py`（3）、`tests/web/test_timeout_flow.py`（4）。另追加 `tests/api/test_config.py` 3 项。
- **M3 测试演进**：`tests/api/test_mcp_integration.py` 的 `next_poll_after` 常量断言改为 settings 推算（演进登记 1，已完成）。
- **规格合规审查**（commit `6399eee`，任务 1）：FR-14b/10.4 逐条吻合，无 BLOCKER/MAJOR。
- **代码质量审查**（commit `6399eee`，任务 1）：竞态裁决/幂等/原子性/先 commit 后入队全部经实证验证，可合入。
- **关键纪律**：LLM 调用只在后台线程（约束 10）；全部状态翻转走条件 UPDATE；LLM 制品全部幂等；送往 LLM 的数据严格 PRD 4.3 白名单；审计不含密钥与提交正文。

## 4. 架构现状

```
个人 Agent（MCP Bearer Token）
   │  list_pending_tasks / get_task / submit_output / get_matter_status
   │  ← 限流：token 60/min + account 120/min（HTTP 429 + Retry-After）
   │  ← submit_output 额外 10/min（ToolError RATE_LIMITED）
   ▼
FastAPI（Web 路由 + MCP 子应用挂载 /mcp）
   │  submit 成功 → maybe_drive_round → drive_queue
   │  拍板动作 → resume_queue
   │  换人 → reassign_task（原任务 reassigned + 新任务 pending）
   ▼
drive_worker / resume_worker / timeout_worker（asyncio，to_thread，per-matter 锁串行化）
   │  timeout_worker：每 60s 扫描 deadline_at<=now 的 pending 任务
   ▼
LangGraph 事项图（thread_id = matter_id，SqliteSaver 与业务表同库）
   generate_round → summarize → branch ┬→ 追问开新轮（FR-17）
                                       ├→ draft_resolution → 双闸门（interrupt）
                                       └→ after_decision → 归档/驳回开轮
   ▼
DeepSeek（出题 / 摘要+收敛 / 定向追问 / 决议草案；重试 3 次退避）
   ← 注入防护：所有不可信数据段 HTML 实体转义 + 逐条包裹
```

数据：单 SQLite 文件（WAL），业务表 + checkpoint 表同库；审计 append-only。

## 5. 已知遗留与后续建议

| 项 | 处置建议 |
|---|---|
| MCP 限流小阈值 429 验证需真实 uvicorn 进程 | 任务 11 手工冒烟 |
| 取消事项（FR-08 取消分支） | P1，非 M4 范围 |
| 登录限流、Web CSRF | P1/P2 |
| 指标看板（Prometheus 类） | P2 |
| PostgreSQL / 多实例部署 | P2 |
| `SCHEDULER_STATE` 无锁读写（CPython GIL 下单属性原子，可接受） | 观测态，无需修复 |
| `_int_env` 对非法 env 值报裸 ValueError（暴露面扩大但与既有行为一致） | 后续包一层带变量名报错 |

## 6. 关键文档索引

- PRD（契约基准）：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-prd.md`
- 设计文档：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-design.md`
- M3 计划：`docs/superpowers/plans/2026-08-12-m3-resolution-gate.md`
- M4 计划：`docs/superpowers/plans/2026-08-13-m4-hardening.md`
- 部署文档：`docs/ops/deployment.md`
- 本说明：`docs/progress/2026-08-13-m4-progress-report.md`
