# 云端多 Agent 协作测试 - 快速开始

本目录包含云端多 Agent 协作测试的完整工具和文档。

---

## 📁 文件说明

| 文件 | 用途 |
|------|------|
| `cloud_multi_agent_test.py` | 主测试脚本（本地运行） |
| `create_test_accounts.py` | 账号创建脚本（服务器运行） |
| `setup_cloud_test.sh` | 自动化设置脚本（需要 SSH key） |
| `cloud_multi_agent_test_guide.md` | 详细测试指南 |

---

## 🚀 三步快速开始

### 步骤 1：在服务器创建测试账号

SSH 登录到服务器并运行：

```bash
# 登录服务器（需要密码）
ssh ubuntu@170.106.192.161

# 进入项目目录
cd ~/mcp-decision-hub

# 创建测试账号
.venv/bin/python scripts/create_test_accounts.py
```

**保存输出的 Token！** 类似：

```bash
export ALICE_TOKEN='mcp_abc123...'
export BOB_TOKEN='mcp_xyz789...'
```

### 步骤 2：创建决策事项

1. 访问 https://hub.tdp-demo.work/login
2. 使用 `owner_test` / `test-pw-123` 登录
3. 创建新事项：
   - 标题：**技术方案选型测试**
   - 参与人：选择 **smoke_alice** 和 **smoke_bob**
   - 问题：**你倾向哪个方案？为什么？**
4. 点击「开始」

### 步骤 3：运行测试脚本

在本地项目目录运行：

```bash
# 设置 Token（使用步骤 1 保存的值）
export ALICE_TOKEN='your_alice_token_here'
export BOB_TOKEN='your_bob_token_here'

# 运行测试
uv run python scripts/cloud_multi_agent_test.py \
  --alice-token "$ALICE_TOKEN" \
  --bob-token "$BOB_TOKEN"
```

---

## 📊 预期输出

```
============================================================
云端多 Agent 协作测试
============================================================

1️⃣ 验证 Agent 连接...
✅ Alice 已连接，待办任务: 1
✅ Bob 已连接，待办任务: 1

2️⃣ 获取待办任务...
  Alice: 1 个任务
  Bob: 1 个任务

3️⃣ 获取任务详情...
📋 Alice 的任务:
  - 事项: 技术方案选型测试
  - 轮次: 1
  - 问题数: 1
    • 你倾向哪个方案？为什么？

4️⃣ 提交立场...
[Alice] 提交立场...
  - 回答数量: 1
  - content_hash: 3f4a8b2c...
  ✅ Alice 提交成功: accepted

[Bob] 提交立场...
  - 回答数量: 1
  - content_hash: 9e8d7c6b...
  ✅ Bob 提交成功: accepted

5️⃣ 等待云端处理...
  [1] 状态: collecting, 轮次: 1
  [2] 状态: summarizing, 轮次: 1
  [3] 状态: awaiting_decision, 轮次: 1

6️⃣ 处理结果...
最终状态: awaiting_decision
总轮次: 1

============================================================
第 1 轮摘要
============================================================

收敛度: high

✅ 共识点:
  - 两人都认为需要权衡稳定性和成本
  - 都同意进度是关键风险

⚡ 分歧点:
  - Alice 更关注稳定性
  - Bob 更关注成本

============================================================
测试完成
============================================================
```

---

## 🔧 高级用法

### 自定义回答内容

```bash
uv run python scripts/cloud_multi_agent_test.py \
  --alice-token "$ALICE_TOKEN" \
  --bob-token "$BOB_TOKEN" \
  --alice-answer "我认为方案 A 更稳定，虽然成本高 15%，但风险可控" \
  --bob-answer "我倾向方案 B，成本优势明显，风险可以通过加强测试覆盖"
```

### 指定事项 ID

如果有多个待办事项：

```bash
uv run python scripts/cloud_multi_agent_test.py \
  --alice-token "$ALICE_TOKEN" \
  --bob-token "$BOB_TOKEN" \
  --matter-id "matter-abc123"
```

---

## ✅ 验证要点

测试成功需要确认：

- [x] 两个 Agent 都能连接到云端
- [x] 立场提交成功（content_hash 验证通过）
- [x] 云端 LLM 生成摘要
- [x] 摘要包含：共识点、分歧点、盲区、追问
- [x] Alice 看不到 Bob 的原始立场（只能看摘要）
- [x] Bob 看不到 Alice 的原始立场（只能看摘要）

---

## 📚 更多文档

详细的测试指南、常见问题、测试用例清单，请参考：

👉 [cloud_multi_agent_test_guide.md](../docs/testing/cloud_multi_agent_test_guide.md)

---

## 🐛 问题排查

### Token 无效

重新生成 Token：
```bash
ssh ubuntu@170.106.192.161
cd ~/mcp-decision-hub
.venv/bin/python scripts/create_test_accounts.py
```

### 云端一直 collecting

查看服务器日志：
```bash
ssh ubuntu@170.106.192.161
tail -100 ~/mcp-decision-hub/nohup.out
```

### Content hash 不匹配

使用脚本提供的 `compute_content_hash` 函数，不要手动计算。

---

**创建时间**: 2026-10-03  
**适用环境**: hub.tdp-demo.work
