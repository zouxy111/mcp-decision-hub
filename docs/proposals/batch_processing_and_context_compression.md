# 集团战略决策会 AI 辅助共识方案

**场景**: 集团战略决策会议 - AI 辅助多方达成共识  
**日期**: 2026-10-03  
**状态**: 方案建议稿

---

## 一、应用场景

### 典型会议流程

**会前准备** → **分散思考** → **集中讨论** → **达成共识** → **形成决议**

```
各位高管/业务负责人
         ↓
   本地与 AI Agent 交互
   （隐私保护，模型不出本地）
         ↓
   提交结论性观点（纯文本）
         ↓
        云端平台
   （AI 分析 + 状态机管理）
         ↓
   生成会议摘要 + 追问
         ↓
   分发回各参与人 Agent
         ↓
   多轮收敛 → 决策拍板
```

---

## 二、核心优化设计

### 2.1 批量处理 + 定期分发（新增）

**问题**：  
当前设计是"来一个处理一个"，参与人多时会频繁触发 LLM 运算。

**优化方案**：

#### 方案 A：时间窗口批处理（推荐）

```
时间轴：
09:00  会议开始，发起议题
09:05  Alice 提交观点
09:08  Bob 提交观点
09:12  Carol 提交观点
       ↓
       [等待窗口：15分钟]
       ↓
09:15  批量处理 Alice + Bob + Carol
       LLM 一次性生成摘要
       ↓
09:16  通过 MCP 分发给所有 Agent
```

**实现要点**：
- 设置时间窗口（如 15 分钟）
- 窗口内累积立场提交
- 窗口结束时批量调用 LLM
- 生成一份综合摘要
- 通过 MCP `get_matter_status` 工具分发

**配置参数**：
```python
BATCH_WINDOW_MINUTES = 15  # 批处理窗口
MAX_BATCH_SIZE = 50        # 单批最大处理数
```

#### 方案 B：人数阈值触发

```
预设参与人数：10 人

提交进度：
Alice   ✅
Bob     ✅
Carol   ✅
...
[已提交 7/10]  → 等待中
[已提交 10/10] → 立即处理
```

**触发条件**：
- 达到预设人数 → 立即处理
- 超时（如 30 分钟）→ 强制处理已提交的

#### 方案 C：混合模式（最灵活）

```
触发条件（满足任一）：
1. 时间窗口到期（15分钟）
2. 收齐所有人（100%）
3. 达到阈值人数（80%）且超时（5分钟）
```

### 2.2 状态机优化

**当前状态机**（已有）：

```
collecting          收集立场
   ↓
in_progress        LLM 生成摘要
   ↓
[判定收敛度]
   ↓
high → awaiting_decision   等待拍板
low  → collecting          下一轮
```

**优化后状态机**（新增批处理）：

```
collecting_batch        收集中（批处理模式）
   ↓
batch_queued           已入队，等待窗口
   ↓
[窗口结束 or 收齐]
   ↓
batch_processing       批量处理中
   ↓
generating_summary     LLM 运算
   ↓
distributing          MCP 分发
   ↓
[收敛判定]
   ↓
high → awaiting_decision
low  → collecting_batch（下一轮）
```

### 2.3 上下文压缩存储

**问题**：  
多轮对话后，上下文会很长，每次都传完整历史给 LLM 成本高。

**优化方案**：

#### 增量摘要 + 层级压缩

```
第 1 轮：10 个人 × 200 字 = 2000 字
   ↓
摘要 1：压缩到 500 字
   ↓
第 2 轮：10 个人 × 200 字 = 2000 字
   ↓
摘要 2：基于摘要 1 + 新内容 = 800 字
   ↓
第 3 轮：10 个人 × 200 字 = 2000 字
   ↓
摘要 3：基于摘要 2 + 新内容 = 1000 字
```

**存储结构**：

```python
RoundSummary 表（已有）:
  - consensus_points      共识点
  - divergences          分歧点
  - blind_spots          盲区
  - open_questions       追问
  - convergence          收敛度

新增字段：
  - compressed_context   压缩后的上下文（用于下一轮）
  - compression_ratio    压缩比（监控质量）
  - token_count          Token 数量
```

**压缩策略**：

1. **去重**：相同观点只保留一次
2. **归类**：按主题聚合相似观点
3. **提炼**：将冗长表述精简为核心要点
4. **分层**：核心共识 > 主要分歧 > 细节讨论

**示例**：

```
原始（2000字）：
Alice: 我认为应该优先发展 AI 业务，理由是市场趋势...（200字）
Bob: 我同意 AI 是重点，但要注意风险...（200字）
Carol: AI 确实重要，我们需要加大投入...（200字）
...

压缩后（500字）：
【共识】全体认同 AI 业务为战略重点
【分歧】投入节奏（激进 vs 稳健）
【盲区】团队能力评估缺失
【追问】预算上限？风险应对？
```

---

## 三、MCP 协议分发机制

### 3.1 当前机制（拉取式）

```
Agent 主动调用：
  list_pending_tasks()  → 查询待办
  get_task(task_id)     → 获取详情
  submit_output(...)    → 提交立场
  get_matter_status()   → 查询摘要
```

**问题**：Agent 需要频繁轮询

### 3.2 优化机制（推拉结合）

#### 被动通知（推送式）

```
云端生成摘要后：
  1. 更新状态机（batch_processing → distributing）
  2. 标记所有参与人的 Task 状态为 "ready"
  3. Agent 轮询时立即发现有新内容
  4. 获取最新摘要和追问
```

**实现**：利用现有 `Task.status` 字段

```python
# 云端批处理完成后
for participant in matter.participants:
    task = create_task(
        participant_id=participant.id,
        round_number=next_round,
        status="ready"  # 新状态：有内容待查看
    )
```

**Agent 端**：

```python
# 定期轮询（如每 5 分钟）
tasks = await client.call_tool("list_pending_tasks", {})

for task in tasks.data["tasks"]:
    if task["status"] == "ready":
        # 有新内容，获取并展示给用户
        detail = await client.call_tool("get_task", {"task_id": task["task_id"]})
        show_to_user(detail.data["previous_summary"])
```

#### 主动推送（可选增强）

**方案 1：Webhook**

```python
# 会议配置时设置回调 URL
matter.webhook_url = "https://corp-agent.example.com/hooks/decision"

# 云端处理完成后
requests.post(
    matter.webhook_url,
    json={
        "matter_id": matter.id,
        "round_number": round.round_number,
        "summary": summary_data,
        "action": "new_summary_available"
    }
)
```

**方案 2：Server-Sent Events (SSE)**

```python
# Agent 订阅事件流
GET /api/matters/{matter_id}/events
Accept: text/event-stream

# 服务器推送
data: {"type": "summary_ready", "round": 2}
```

---

## 四、技术实现路径

### 4.1 批处理模块（新增）

```python
# hub/api/batch_processor.py

class BatchProcessor:
    """批处理收集和调度器"""
    
    def __init__(self, window_minutes: int = 15):
        self.window_minutes = window_minutes
        self.pending_batches = {}  # {matter_id: [submissions]}
    
    def add_submission(self, matter_id: str, task_id: str):
        """添加待处理提交"""
        if matter_id not in self.pending_batches:
            # 首次提交，启动定时器
            self.pending_batches[matter_id] = {
                "submissions": [],
                "window_start": utcnow(),
                "timer": self._schedule_batch(matter_id)
            }
        self.pending_batches[matter_id]["submissions"].append(task_id)
        
        # 检查是否已收齐
        if self._is_complete(matter_id):
            self._trigger_batch(matter_id)
    
    def _schedule_batch(self, matter_id: str):
        """安排批处理任务"""
        timer = Timer(
            self.window_minutes * 60,
            self._trigger_batch,
            args=[matter_id]
        )
        timer.start()
        return timer
    
    def _trigger_batch(self, matter_id: str):
        """触发批处理"""
        if matter_id not in self.pending_batches:
            return
        
        batch = self.pending_batches.pop(matter_id)
        
        # 提交到后台处理队列
        background_queue.enqueue(
            "process_batch",
            matter_id=matter_id,
            submissions=batch["submissions"]
        )
```

### 4.2 上下文压缩（新增）

```python
# hub/llm/context_compressor.py

class ContextCompressor:
    """上下文压缩器"""
    
    def compress_round_history(
        self,
        previous_summaries: list[dict],
        current_submissions: list[dict],
        max_tokens: int = 2000
    ) -> str:
        """
        压缩历史摘要 + 当前提交
        
        策略：
        1. 最新一轮摘要保留完整
        2. 更早的轮次只保留核心共识和主要分歧
        3. 当前提交去重归类
        """
        compressed = []
        
        # 历史摘要分层压缩
        if len(previous_summaries) > 1:
            # 旧轮次：只保留核心
            for summary in previous_summaries[:-1]:
                compressed.append({
                    "consensus": summary["consensus_points"][:3],  # 前3个
                    "divergences": summary["divergences"][:2]      # 前2个
                })
        
        # 上一轮：完整保留
        if previous_summaries:
            compressed.append(previous_summaries[-1])
        
        # 当前轮次：去重归类
        current_compressed = self._deduplicate_submissions(current_submissions)
        
        return self._format_for_llm(compressed, current_compressed)
    
    def _deduplicate_submissions(self, submissions: list[dict]) -> list[dict]:
        """去重相似观点"""
        # 使用语义相似度聚类
        clusters = cluster_by_similarity(submissions, threshold=0.85)
        
        # 每个簇选代表性观点
        return [cluster.representative for cluster in clusters]
```

### 4.3 分发机制（增强现有）

```python
# hub/api/distribution.py

async def distribute_summary_to_agents(
    session: Session,
    matter_id: str,
    round_number: int
):
    """摘要生成后分发给所有 Agent"""
    
    matter = session.get(Matter, matter_id)
    participants = session.scalars(
        select(MatterParticipant).where(
            MatterParticipant.matter_id == matter_id
        )
    ).all()
    
    # 创建下一轮任务
    for participant in participants:
        task = Task(
            matter_id=matter_id,
            participant_id=participant.id,
            round_id=next_round.id,
            status="ready",  # 标记为有内容待查看
            created_at=utcnow()
        )
        session.add(task)
    
    session.commit()
    
    # 可选：Webhook 通知
    if matter.webhook_url:
        await notify_webhook(matter.webhook_url, {
            "matter_id": matter_id,
            "round_number": round_number,
            "action": "summary_available"
        })
```

---

## 五、配置建议

### 5.1 会议规模配置

| 会议规模 | 参与人数 | 批处理窗口 | 压缩阈值 |
|---------|---------|-----------|---------|
| 小型 | 3-5人 | 5分钟 | 不压缩 |
| 中型 | 6-15人 | 15分钟 | 3轮后压缩 |
| 大型 | 16-50人 | 30分钟 | 2轮后压缩 |

### 5.2 环境变量

```bash
# .env
BATCH_PROCESSING_ENABLED=true
BATCH_WINDOW_MINUTES=15
BATCH_MAX_SIZE=50
BATCH_TRIGGER_THRESHOLD=0.8  # 80% 提交即可触发

CONTEXT_COMPRESSION_ENABLED=true
CONTEXT_MAX_TOKENS=2000
COMPRESSION_START_ROUND=3

WEBHOOK_ENABLED=false
WEBHOOK_TIMEOUT_SECONDS=5
```

---

## 六、实施路线图

### Phase 1：批处理基础（2周）

- [ ] 实现批处理调度器
- [ ] 修改状态机增加批处理状态
- [ ] 调整 `maybe_drive_round` 支持批处理
- [ ] 单元测试 + 集成测试

### Phase 2：上下文压缩（2周）

- [ ] 实现压缩算法
- [ ] 扩展 `RoundSummary` 表结构
- [ ] 调整 prompt 模板支持压缩上下文
- [ ] 性能测试（Token 节省率）

### Phase 3：分发优化（1周）

- [ ] 增加 `Task.status = "ready"` 状态
- [ ] 优化 Agent 轮询逻辑
- [ ] 可选：Webhook 通知
- [ ] 端到端测试

### Phase 4：生产验证（1周）

- [ ] 灰度发布（小规模会议）
- [ ] 监控指标：批处理延迟、压缩比、分发时间
- [ ] 收集反馈优化
- [ ] 全量上线

---

## 七、预期效果

### 7.1 性能提升

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| LLM 调用次数 | 每人一次 | 批次一次 | ↓ 90% |
| 处理延迟 | 立即 | 窗口期 | +15 分钟 |
| Token 消耗 | 线性增长 | 压缩增长 | ↓ 60% |
| 并发压力 | 高 | 低 | ↓ 85% |

**示例**（10人会议）：

```
优化前：
- 10 次 LLM 调用
- 每次 2000 tokens
- 总计：20,000 tokens
- 并发峰值：10 requests/min

优化后：
- 1 次 LLM 调用（批处理）
- 压缩上下文：8000 tokens
- 总计：8,000 tokens
- 并发峰值：1 request/15min
```

### 7.2 用户体验

- ✅ **异步友好**：不需要所有人同时在线
- ✅ **节奏可控**：批处理窗口给思考时间
- ✅ **信息对称**：统一时间点收到摘要
- ✅ **隐私保护**：本地模型 + 云端摘要不变

---

## 八、风险与应对

### 8.1 延迟风险

**风险**：批处理窗口导致等待时间增加

**应对**：
- 提供"快速模式"开关（小会议可关闭批处理）
- 动态调整窗口（收齐即处理）
- 前端显示进度条（已提交 X/Y 人）

### 8.2 压缩质量风险

**风险**：过度压缩丢失关键信息

**应对**：
- 设置压缩比监控（< 50% 告警）
- 关键轮次（如第1轮）不压缩
- 人工审核机制（Owner 可查看原文）

### 8.3 系统复杂度

**风险**：新增模块增加维护成本

**应对**：
- 功能开关（可随时降级到原模式）
- 完整的单元测试和集成测试
- 详细的运维文档和监控面板

---

## 九、总结

### 核心改进

1. **批量处理**：从"来一个处理一个"变为"攒一批处理一次"
2. **上下文压缩**：多轮对话不会无限膨胀，保持 Token 可控
3. **定期分发**：通过 MCP 协议的 Task 状态机制，让 Agent 感知新内容

### 技术优势

- ✅ 基于现有架构扩展，不推翻重来
- ✅ LangGraph 状态机天然支持批处理
- ✅ MCP 协议标准化，Agent 接入简单
- ✅ 增量实施，风险可控

### 业务价值

- 🎯 **效率**：LLM 调用减少 90%，成本降低
- 🎯 **体验**：异步协作，不受时区/日程限制
- 🎯 **质量**：AI 辅助共识，减少无效争论
- 🎯 **隐私**：本地决策模型，云端只处理结论

---

**建议**：先在小规模会议（5-8人）试点，验证批处理窗口和压缩策略，再推广到大型战略会。

**预估工期**：6周（完整实施 Phase 1-4）

**投入产出比**：高（一次开发，长期受益）
