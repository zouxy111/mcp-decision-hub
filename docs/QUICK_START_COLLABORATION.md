# MCP 决策中台 - 多人协作快速开始

## 5 分钟上手指南

### 作为领导/组织者

#### 1. 启动项目协作
```python
# 通过本地 Agent 对话
领导: "我要开始一个产品研发项目，邀请张三、李四、王五"

# Agent 自动执行：
# 1. 创建项目事项
# 2. 为每个人生成邀请链接
# 3. 返回可分享的链接
```

**生成的邀请链接示例**：
```
张三: https://your-domain.com/invite/xY9kL2 (10次使用，3天有效)
李四: https://your-domain.com/invite/aB3cD4 (10次使用，3天有效)
王五: https://your-domain.com/invite/eF5gH6 (10次使用，3天有效)
```

#### 2. 启动会议协作
```python
领导: "组织一个季度规划会议，需要多人快速加入"

# Agent 自动执行：
# 1. 创建会议事项
# 2. 生成无限制邀请链接
# 3. 返回可群发的链接
```

**生成的邀请链接示例**：
```
会议邀请: https://your-domain.com/invite/mN7pQ8 (无限使用，今日有效)
```

---

### 作为成员/参与者

#### 1. 点击邀请链接
在浏览器中打开收到的邀请链接，或：

```bash
# 通过本地 Agent CLI
mcp-decision-hub join https://your-domain.com/invite/xY9kL2
```

#### 2. 回答引导问卷
Agent 会询问：
- **姓名**：张三
- **邮箱**：zhangsan@example.com
- **用户名**：zhangsan（自动生成，可修改）
- **密码**：（自动生成，可修改）

#### 3. 自动完成配置
Agent 会：
- ✅ 创建云端账号
- ✅ 加入协作事项
- ✅ 配置本地 MCP 连接
- ✅ 同步事项信息

#### 4. 开始协作
```bash
# 查看当前事项
mcp-decision-hub matters list

# 提交立场
mcp-decision-hub stance submit "我的立场是..."

# 查看进度
mcp-decision-hub matters show <matter-id>
```

---

## 两种协作模式对比

| 特性 | 项目协作 (project) | 会议协作 (meeting) |
|------|-------------------|-------------------|
| **适用场景** | 长期项目（产品研发、业务规划） | 短期会议（季度会、研讨会） |
| **邀请方式** | 为每个人生成独立链接 | 一个链接供所有人使用 |
| **使用次数** | 有限制（如 10 次） | 无限制 |
| **有效期** | 较长（3-7 天） | 较短（当天或次日） |
| **成员加入** | 逐个邀请，可指定姓名 | 群发链接，快速加入 |
| **会后管理** | 长期保留，持续协作 | 结束后撤销邀请链接 |

---

## 常见操作

### 查看邀请链接状态
```python
领导: "查看产品研发项目的邀请链接"

Agent: 
  邀请链接 xY9kL2 (张三)
    - 状态: active
    - 已使用: 1/10 次
    - 有效期: 2026-10-09
  
  邀请链接 aB3cD4 (李四)
    - 状态: consumed (已用尽)
    - 已使用: 10/10 次
    - 有效期: 2026-10-09
```

### 撤销邀请链接
```python
领导: "撤销会议邀请链接 mN7pQ8"

Agent: ✅ 邀请链接已撤销，不再接受新成员加入
      已加入的 15 位成员可继续访问会议内容
```

### 重新生成邀请
```python
领导: "为赵六重新生成邀请链接"

Agent: ✅ 新邀请链接: https://your-domain.com/invite/pR9sT0
      (10次使用，3天有效)
```

---

## 安全提示

### ✅ 推荐做法
- 为不同成员生成独立链接（项目协作）
- 设置合理的有效期（不要过长）
- 会议结束后及时撤销邀请
- 定期检查邀请使用情况

### ❌ 避免做法
- 不要在公开渠道分享邀请链接
- 不要使用过长的有效期（如 30 天）
- 不要忘记撤销临时会议的邀请
- 不要将项目邀请设置为无限制使用

---

## API 集成（高级）

### Python SDK
```python
from hub.domain import invitation_links

# 创建项目邀请
invitation = invitation_links.create_invitation_link(
    db=db,
    matter_id="mat_xxx",
    created_by=user_id,
    invited_name="张三",
    max_uses=10,
    expires_in_days=3,
)

# 创建会议邀请
invitation = invitation_links.create_invitation_link(
    db=db,
    matter_id="mat_xxx",
    created_by=user_id,
    max_uses=None,  # 无限制
    expires_in_days=1,
)

# 消费邀请
user, invitation = invitation_links.consume_invitation_link(
    db=db,
    short_code="xY9kL2",
    username="zhangsan",
    email="zhangsan@example.com",
    password_hash=hash_password("password123"),
)

# 撤销邀请
invitation_links.revoke_invitation(
    db=db,
    invitation_id="inv_xxx",
    revoked_by=user_id,
)
```

### MCP 工具调用
```json
// 创建邀请
{
  "tool": "create_invitation",
  "arguments": {
    "matter_id": "mat_xxx",
    "invited_name": "张三",
    "max_uses": 10,
    "expires_in_days": 3
  }
}

// 消费邀请
{
  "tool": "consume_invitation",
  "arguments": {
    "short_code": "xY9kL2",
    "username": "zhangsan",
    "email": "zhangsan@example.com",
    "password": "password123"
  }
}
```

---

## 故障排查

### 问题：邀请链接无效
**可能原因**：
- 链接已过期
- 链接已达到使用次数上限
- 链接已被撤销

**解决方案**：
```python
领导: "检查邀请链接 xY9kL2 的状态"

# 如果过期或用尽，重新生成
领导: "为张三重新生成邀请链接"
```

### 问题：用户名已存在
**可能原因**：
- 该用户已通过其他邀请链接注册

**解决方案**：
```python
# 尝试使用不同的用户名
用户名: zhangsan2
# 或询问领导是否已有账号
```

### 问题：无法加入协作
**可能原因**：
- 邀请链接不匹配（复制错误）
- 网络连接问题

**解决方案**：
```python
# 检查链接完整性
领导: "重新发送邀请链接"

# 或使用短码直接加入
mcp-decision-hub join --code xY9kL2
```

---

## 运行演示

查看完整的协作流程演示：

```bash
# 运行演示脚本
uv run python scripts/demo_invitation_system.py

# 输出：
# ✅ 演示 1：项目协作流程
# ✅ 演示 2：会议协作流程
# ✅ 演示 3：安全特性验证
```

---

## 下一步

- 📖 阅读[完整实现总结](multi-agent-collaboration-summary.md)
- 🔧 查看[技术实现文档](invitation-system-implementation.md)
- 📚 参考[用户使用指南](invitation-system-user-guide.md)
- 🎯 查看[需求分析报告](multi-agent-collaboration-report.md)

---

## 支持

如有问题，请：
1. 查看[常见问题](FAQ.md)
2. 提交 Issue 到 GitHub
3. 联系技术支持团队
