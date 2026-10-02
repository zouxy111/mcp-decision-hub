# 云端多 Agent 协作测试指南

**目标**: 验证两个本地 Agent 通过云端服务器进行决策协作的完整流程

---

## 测试场景

模拟真实的跨地协作场景：

- **Alice** 和 **Bob** 两个用户，各自在本地运行 Agent
- 通过 MCP 协议连接到云端服务器 `https://hub.tdp-demo.work`
- **Owner** 在云端创建决策事项，邀请 Alice 和 Bob 参与
- Alice 和 Bob 各自提交立场（经本人确认的纯文本）
- 云端进行多轮摘要、收敛判定、差异化追问
- Owner 最终拍板

---

## 前置条件

### 1. 云端账号准备

需要在服务器上创建测试账号：

```bash
# SSH 登录到服务器
ssh ubuntu@170.106.192.161

# 进入项目目录
cd ~/mcp-decision-hub

# 运行种子脚本创建测试账号
.venv/bin/python << 'EOF'
from sqlalchemy import select
from hub.api.tokens import issue_token
from hub.config import load_settings
from hub.db.models import User
from hub.db.session import init_db, make_engine, make_session_factory
from argon2 import PasswordHasher

settings = load_settings()
engine = make_engine(settings.database_url)
init_db(engine)
factory = make_session_factory(engine)

USERS = ["smoke_alice", "smoke_bob", "owner_test"]

with factory() as session:
    hasher = PasswordHasher()
    for name in USERS:
        exists = session.scalar(select(User).where(User.username == name))
        if exists is None:
            session.add(
                User(
                    username=name,
                    email=f"{name}@example.com",
                    password_hash=hasher.hash("test-pw-123"),
                    is_admin=False,
                    is_active=True,
                    must_change_password=False
                )
            )
    session.commit()
    
    # 生成 Token
    alice_user = session.scalar(select(User).where(User.username == "smoke_alice"))
    bob_user = session.scalar(select(User).where(User.username == "smoke_bob"))
    
    _, token_a = issue_token(session, user=alice_user, name="test-alice-token")
    _, token_b = issue_token(session, user=bob_user, name="test-bob-token")
    
    session.commit()
    
    print("="*60)
    print("测试账号创建完成")
    print("="*60)
    print(f"\nAlice Token:\n{token_a}")
    print(f"\nBob Token:\n{token_b}")
    print("\n保存这些 Token，后面会用到！")
EOF
```

### 2. Web 创建决策事项

1. 访问 https://hub.tdp-demo.work/login
2. 使用 `owner_test` / `test-pw-123` 登录
3. 点击「创建新事项」
4. 填写事项信息：
   - **标题**: "技术方案选型：方案 A vs 方案 B"
   - **描述**: "需要在稳定性和成本之间权衡"
   - **参与人**: 选择 `smoke_alice` 和 `smoke_bob`
5. 添加首轮问题：
   - "你倾向哪个方案？请说明理由。"
   - "你认为最大的风险是什么？"
6. 点击「开始」

---

## 测试步骤

### 方式 1：使用测试脚本（推荐）

```bash
# 在本地项目目录
cd /path/to/mcp-decision-hub

# 使用服务器返回的 Token 运行测试
uv run python scripts/cloud_multi_agent_test.py \
  --alice-token "alice_的_token_这里" \
  --bob-token "bob_的_token_这里"
```

**脚本会自动**:
1. ✅ 验证两个 Agent 的连接
2. ✅ 获取待办任务列表
3. ✅ 获取任务详情（问题内容）
4. ✅ 提交立场（自动计算 content_hash）
5. ✅ 等待云端处理（轮询状态）
6. ✅ 显示摘要结果（共识、分歧、追问）

**可选参数**:
```bash
# 指定事项 ID（如果有多个待办）
--matter-id "matter-xxx"

# 自定义回答内容
--alice-answer "我认为方案 A 更合适，因为..."
--bob-answer "我倾向方案 B，理由是..."
```

### 方式 2：手动使用 MCP 客户端

如果你想手动测试每一步：

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# Alice 的客户端
alice_client = Client(
    StreamableHttpTransport(
        url="https://hub.tdp-demo.work/mcp/",
        headers={"Authorization": f"Bearer {alice_token}"}
    )
)

async with alice_client as c:
    # 1. 列出待办
    tasks = await c.call_tool("list_pending_tasks", {})
    print(tasks.data)
    
    # 2. 获取任务详情
    detail = await c.call_tool("get_task", {"task_id": task_id})
    print(detail.data)
    
    # 3. 提交立场
    result = await c.call_tool("submit_output", {
        "task_id": task_id,
        "answers": [...],
        "notes": "补充说明",
        "human_approved": True,
        "approved_at": "2026-10-03T12:00:00Z",
        "content_digest": "计算的hash",
        "idempotency_key": "uuid"
    })
```

---

## 预期结果

### 阶段 1：立场收集
```
1️⃣ 验证 Agent 连接...
✅ Alice 已连接，待办任务: 1
✅ Bob 已连接，待办任务: 1

2️⃣ 获取待办任务...
  Alice: 1 个任务
  Bob: 1 个任务

3️⃣ 获取任务详情...
📋 Alice 的任务:
  - 事项: 技术方案选型：方案 A vs 方案 B
  - 轮次: 1
  - 问题数: 2
    • 你倾向哪个方案？请说明理由。
    • 你认为最大的风险是什么？

4️⃣ 提交立场...
[Alice] 提交立场...
  - 回答数量: 2
  - content_hash: 3f4a8b2c1d5e6f7a...
  ✅ Alice 提交成功: accepted

[Bob] 提交立场...
  - 回答数量: 2
  - content_hash: 9e8d7c6b5a4f3e2d...
  ✅ Bob 提交成功: accepted
```

### 阶段 2：云端处理

```
5️⃣ 等待云端处理...
等待云端处理（最多 120 秒，每 5 秒检查一次）...
  [1] 状态: collecting, 轮次: 1
  [2] 状态: collecting, 轮次: 1
  [3] 状态: summarizing, 轮次: 1
  [4] 状态: collecting, 轮次: 2
```

**说明**:
- `collecting`: 收集立场中
- `summarizing`: LLM 正在生成摘要
- 轮次增加表示进入下一轮（说明需要继续追问）

### 阶段 3：查看结果

```
6️⃣ 处理结果...
最终状态: awaiting_decision
总轮次: 2

============================================================
第 1 轮摘要
============================================================

收敛度: medium

✅ 共识点:
  - 两人都认为进度风险是最大的挑战
  - 双方都同意需要在稳定性和成本之间权衡

⚡ 分歧点:
  - Alice 更关注稳定性，Bob 更关注成本
  - 对风险的优先级判断不同

👁️ 盲区:
  - 没有讨论实施时间表
  - 缺少对团队能力的评估

❓ 追问:
  - 如果方案 A 超预算 20%，还会坚持吗？
  - 方案 B 的风险可以通过什么方式降低？

============================================================
第 2 轮摘要
============================================================
...
```

---

## 验证要点

### ✅ 功能验证

1. **连接性**
   - [ ] Alice Agent 能连接到云端
   - [ ] Bob Agent 能连接到云端
   - [ ] Token 认证工作正常

2. **任务分发**
   - [ ] Alice 收到正确的任务
   - [ ] Bob 收到正确的任务
   - [ ] 任务内容包含所有问题

3. **立场提交**
   - [ ] content_hash 计算正确
   - [ ] 服务器验证通过
   - [ ] 幂等性保证（重复提交不报错）

4. **云端处理**
   - [ ] LLM 生成摘要
   - [ ] 收敛判定逻辑
   - [ ] 差异化追问生成

5. **多轮协作**
   - [ ] 自动进入第二轮
   - [ ] 新问题正确分发
   - [ ] 历史摘要可见

### ✅ 隔离性验证

1. **个人决策模型隔离**
   - [ ] Alice 看不到 Bob 的原始立场
   - [ ] Bob 看不到 Alice 的原始立场
   - [ ] 只能看到云端生成的摘要

2. **Token 隔离**
   - [ ] Alice 的 Token 无法操作 Bob 的任务
   - [ ] Bob 的 Token 无法操作 Alice 的任务

3. **本地 Agent 隔离**
   - [ ] Alice 的决策模型在本地
   - [ ] Bob 的决策模型在本地
   - [ ] 云端只收到确认后的文本

---

## 常见问题

### Q1: 提示 "content_hash 不匹配"

**原因**: 客户端和服务器端计算 hash 的方式不一致

**解决**: 
- 确保使用脚本中的 `compute_content_hash` 函数
- 检查 `answers` 是否按 `question_id` 排序
- 检查 JSON 序列化参数（`sort_keys=True, ensure_ascii=False`）

### Q2: 提示 "rate limit exceeded"

**原因**: 提交频率过高

**解决**:
- 等待 1 分钟后重试
- 检查 PRD 9.1 配额限制

### Q3: 云端一直在 `collecting` 状态

**可能原因**:
1. 还有参与人未提交
2. 后台任务队列堵塞
3. DeepSeek API 调用失败

**排查**:
```bash
# SSH 到服务器查看日志
ssh ubuntu@170.106.192.161
cd ~/mcp-decision-hub
tail -100 nohup.out
# 或
sudo journalctl -u mcp-decision-hub -n 100
```

### Q4: Token 无效

**原因**: Token 可能被撤销或过期

**解决**: 重新生成 Token（参考前置条件步骤）

---

## 测试用例清单

### 基础流程测试

- [ ] **TC01**: 两人提交，进入第一轮摘要
- [ ] **TC02**: 收敛度高，直接进入 awaiting_decision
- [ ] **TC03**: 收敛度低，进入第二轮追问
- [ ] **TC04**: 达到最大轮次（6轮），进入 blocked

### 边界测试

- [ ] **TC05**: 只有 Alice 提交，Bob 不提交（超时机制）
- [ ] **TC06**: Alice 提交后撤回重新提交（幂等性）
- [ ] **TC07**: 同时提交（并发处理）
- [ ] **TC08**: 大量文本立场（字数限制）

### 隔离性测试

- [ ] **TC09**: Alice 尝试读取 Bob 的原始立场（应失败）
- [ ] **TC10**: 使用错误的 Token（应拒绝）
- [ ] **TC11**: 跨事项访问（应隔离）

---

## 成功标准

测试通过需满足：

1. ✅ 两个 Agent 都能正常连接云端
2. ✅ 立场提交成功，content_hash 验证通过
3. ✅ 云端 LLM 生成摘要
4. ✅ 摘要包含：共识点、分歧点、盲区、追问
5. ✅ 收敛判定合理（high → 决策，low → 下一轮）
6. ✅ 参与人只能看到摘要，看不到对方原始立场
7. ✅ Owner 能在最终拍板

---

## 下一步

测试通过后，你可以：

1. **部署生产环境**: 参考 [deployment_status_20261003.md](deployment_status_20261003.md)
2. **接入真实 Agent**: 使用 Skills 分发点提供的安装包
3. **配置告警监控**: 设置任务超时、API 失败告警
4. **优化用户体验**: 前端实时显示协作进度

---

**文档版本**: v1.0  
**创建时间**: 2026-10-03  
**适用环境**: hub.tdp-demo.work (生产测试服)
