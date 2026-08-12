# MCP 决策中台 · 项目进度说明

- 日期：2026-08-12
- 仓库：`/Volumes/ZXSSD/work/公司项目/mcp-decision-hub`（main 分支直接开发）
- 当前状态：**M1 / M2 / M3 三个里程碑全部完成**，380 个自动化测试全绿，真实 DeepSeek 端到端冒烟通过

---

## 1. 一句话总览

协作决策中台已完成"账号与身份 → 多 Agent 多轮收敛讨论 → 决议闸门与拍板归档"的完整产品闭环：发起人创建事项，平台用 DeepSeek 出题，各参与人的 Agent 经 MCP 独立作答，平台逐轮摘要并判断收敛，收敛后自动生成决议草案，发起人在 Web 上拍板（通过/修改通过/驳回），拍板并发由版本乐观锁保护，全过程审计可查、崩溃可恢复。

## 2. 里程碑交付情况

| 里程碑 | 内容 | 状态 | 测试基线 | 关键证据 |
|---|---|---|---|---|
| M1 骨架 | 账号/邀请/Token/事项/4 个 MCP 方法/人审三件套/幂等六象限 | ✅ 完成（22 commits） | 149 passed | M1 终审通过 |
| M2 串联 | DeepSeek 出题/摘要/四态收敛/多轮追问/后台管线/重启恢复 | ✅ 完成（18 任务） | 257 passed | 真实 DeepSeek 两轮闭环冒烟通过 |
| M3 闸门 | LangGraph 编排/决议草案/拍板/version 乐观锁/Web 闭环/审计查询 | ✅ 完成（18 任务） | **380 passed** | 真实 DeepSeek 决议闭环冒烟通过 |

当前 HEAD：`683ef08`（66 commits 总数）。代码规模：hub 约 4600 行，tests 约 6900 行，52 个测试文件。

## 3. M3（本轮）交付明细

### 3.1 功能落地

- **决议草案（FR-19）**：收敛（converged / provisionally_ready）后自动基于全部轮次摘要生成草案（建议/依据/风险/分歧/引用轮次），草案 prompt 全条目数据段包裹防注入；轮次上限 blocked 时发起人可手动直接生成草案（PRD 7.5）。
- **拍板（FR-20/21/21b）**：仅发起人；通过/修改通过/驳回三动作；version+status 双条件 UPDATE 乐观锁——并发拍板只有一个成功，败者 `409 RESOLUTION_VERSION_CONFLICT` 且不写入；三终态 version 均递增；驳回必填理由并自动开新轮（达上限时隐含 +1 额度，理由入审计截断 500）。
- **provisional 二选一（PRD 7.6）**：暂定收敛时草案生成并暂停，发起人可选"继续追问"或"进入拍板"，授信只在图节点内幂等完成。
- **LangGraph 编排（设计 §6）**：langgraph 1.2.11 + SqliteSaver，checkpoint 与业务表同 SQLite 文件；图节点为 M2 相位函数薄封装；双闸门 interrupt/resume（provisional_gate / decision_gate）；两个已验证 footgun 均已锁定进测试。
- **重启恢复（FR-24）**：双 reconciler（轮次中断 + 已决未传播决议）+ checkpoint 恢复，冒烟实测"闸门暂停态重启后完整恢复"。
- **Web 闭环**：拍板页（三动作表单 + 版本冲突"请重新加载后再操作"提示）、详情页决议卡片/处理中态/失败重试按钮/只读完成态。
- **审计查询（FR-23b）**：`/admin/audit` 管理员五维筛选 + 分页；`/matters/{id}/audit` 发起人本事项全量；参与人 403 + 审计；只读无写路由。
- **MCP（PRD 9.2）**：`get_matter_status.resolution` 返回六键元数据（无正文，从严数据面）。

### 3.2 真实 DeepSeek 冒烟（任务 18）

deepseek-v4-flash，独立冒烟库（未入库）。验证通过：完整闭环（provisional→进入拍板→通过→completed，草案质量高）；驳回开新轮（旧草案只读 v2）；闸门暂停态重启恢复；版本冲突 409 且不覆盖；管理员/发起人审计叙事链完整。插曲：罐头答案不切题时 DeepSeek 合理判 blocked（连续无进展），属 LLM 判定正确。

### 3.3 用户拍板的四个口径（不可回退）

1. 三种拍板终态（approved/modified/rejected）version 都递增；
2. 驳回仅在已达轮次上限时隐含授信 +1；
3. 补上"轮次上限 blocked 直接生成决议草案"入口；
4. 拍板/驳回理由写入审计 detail（截断 500 字符）。

## 4. 工程质量

- **测试**：380 passed / 0 failed（uv run pytest tests -q）；ruff check（E/F/I）零错误；e2e 验收场景测试覆盖 PRD §13 的 1/6/7/9/16/17/19/24 场景，经 deflake 后连续 12 轮全绿。
- **流程**：全部功能任务走 subagent-driven-development（实现 → 规格合规审查 → 代码质量审查），M2/M3 各有最终里程碑级整体审查；多处 DONE_WITH_CONCERNS 偏离均经独立实证为真实必要（陈旧读、规格取数 bug、LangGraph 语义坑等），计划文档同步入库。
- **关键纪律**：LLM 调用只在后台线程（约束 10）；全部状态翻转走条件 UPDATE；LLM 制品全部幂等；送往 LLM 的数据严格 PRD 4.3 白名单；审计不含密钥与提交正文。

## 5. 架构现状

```
个人 Agent（MCP Bearer Token）
   │  list_pending_tasks / get_task / submit_output / get_matter_status
   ▼
FastAPI（Web 路由 + MCP 子应用挂载 /mcp）
   │  submit 成功 → maybe_drive_round → drive_queue
   │  拍板动作 → resume_queue
   ▼
drive_worker / resume_worker（asyncio，to_thread）
   ▼
LangGraph 事项图（thread_id = matter_id，SqliteSaver 与业务表同库）
   generate_round → summarize → branch ┬→ 追问开新轮（FR-17）
                                       ├→ draft_resolution → 双闸门（interrupt）
                                       └→ after_decision → 归档/驳回开轮
   ▼
DeepSeek（出题 / 摘要+收敛 / 定向追问 / 决议草案；重试 3 次退避）
```

数据：单 SQLite 文件（WAL），业务表 + checkpoint 表同库；审计 append-only。

## 6. 已知遗留与技术债（M4 候选）

| 项 | 处置建议 |
|---|---|
| FR-14b 超时调度器（任务超时判定） | M4 主线 |
| FR-08b 换人 | M4 主线 |
| 限流（429 实计数、next_poll_after） | M4 |
| M2 摘要 prompt 的 previous_summary 平铺二阶注入窗口 | M4 安全项（M3 决议 prompt 已逐条包裹） |
| 注入闭合标记伪造（`</user_submitted_content>` 逃逸） | M4 安全项（转义/nonce 标记） |
| resume 与 tick 并发窗口（双 worker 同 matter） | 可接受，M4 可串行化 |
| e2e 加固、指标、备份与部署文档 | M4 主线 |

## 7. 下一步

**M4 加固**（PRD 12.2）：超时调度器、换人、限流、e2e 加固、指标、备份和部署文档。执行前沿用本次模式：先写 M4 计划并入库 → 逐任务 TDD + 双审查 → 真实冒烟。

## 8. 关键文档索引

- PRD（契约基准）：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-prd.md`
- 设计文档：`docs/superpowers/specs/2026-08-11-mcp-decision-hub-design.md`
- 进度交接（含 M3 完成摘要）：`docs/superpowers/specs/2026-08-11-m2-progress-handoff.md`
- M2 计划：`docs/superpowers/plans/2026-08-11-m2-llm-pipeline.md`
- M3 计划：`docs/superpowers/plans/2026-08-12-m3-resolution-gate.md`
- 本说明：`docs/progress/2026-08-12-progress-report.md`
