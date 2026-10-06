# 邀请链接系统使用指南

## 目录
1. [快速开始](#快速开始)
2. [项目协作场景](#项目协作场景)
3. [会议协作场景](#会议协作场景)
4. [管理邀请链接](#管理邀请链接)
5. [常见问题](#常见问题)

---

## 快速开始

### 什么是邀请链接？

邀请链接是一种简单、安全的方式，让您可以邀请其他人加入您的项目或会议。受邀者点击链接后，可以快速注册账号并自动加入协作。

### 主要特点

✅ **简单快捷**：一个链接，完成注册和加入  
✅ **安全可控**：支持过期时间、使用次数限制  
✅ **灵活管理**：随时撤销不需要的邀请  
✅ **双模式**：支持项目协作和会议协作

---

## 项目协作场景

### 适用场景

- 长期项目合作
- 需要明确角色分工
- 需要了解成员背景

### 创建项目邀请

#### 1. 通过 API 创建

```bash
curl -X POST https://your-domain.com/api/invitations/create \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "matter_id": 123,
    "expires_in_days": 7,
    "max_uses": 10,
    "invited_name": "张三"
  }'
```

#### 2. 通过 MCP 工具创建

```python
# 在本地 AI 对话中
"请为项目 123 创建一个邀请链接，有效期 7 天，最多 10 人使用"

# AI 会调用 create_invitation_link 工具
```

#### 3. 返回结果

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "short_code": "abc12345",
  "full_url": "/invite/abc12345",
  "expires_at": "2024-01-08T00:00:00Z",
  "max_uses": 10,
  "used_count": 0,
  "status": "active",
  "is_active": true
}
```

### 接受项目邀请

#### 受邀者操作流程

1. **打开邀请链接**
   ```
   https://your-domain.com/invite/abc12345
   ```

2. **查看项目信息**
   - 项目名称
   - 发起人
   - 协作模式
   - 过期时间

3. **填写注册信息**
   ```
   基本信息：
   - 用户名：zhangsan
   - 邮箱：zhangsan@example.com
   - 密码：******
   - 确认密码：******
   
   项目协作问卷：
   - 您的专业背景是什么？
     软件工程师，5年后端开发经验
   
   - 您在项目中期望承担什么角色？
     技术负责人，负责架构设计
   
   - 您对项目的期望是什么？
     希望学习分布式系统设计
   ```

4. **提交并完成注册**
   - 系统自动创建账号
   - 自动加入项目
   - 重定向到登录页

5. **登录开始协作**
   ```
   用户名：zhangsan
   密码：******
   ```

---

## 会议协作场景

### 适用场景

- 临时性会议
- 快速决策
- 实时立场同步

### 创建会议邀请

#### 1. 先创建会议事项

```python
# 通过 API 或 MCP 工具
matter = create_matter(
    title="Q1 产品规划会议",
    goal="确定 2024 Q1 产品路线图",
    mode="meeting",  # 关键：指定为会议模式
    background="需要讨论新功能优先级"
)
```

#### 2. 生成会议邀请链接

```bash
curl -X POST https://your-domain.com/api/invitations/create \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "matter_id": 456,
    "expires_in_days": 1,
    "max_uses": null,
    "invited_name": null
  }'
```

**注意**：
- `expires_in_days`: 会议链接通常有效期较短（1天）
- `max_uses`: null 表示无限制，方便多人快速加入
- `invited_name`: 可以不指定，让参与者自己填写

### 接受会议邀请

#### 受邀者操作流程

1. **打开邀请链接**
   ```
   https://your-domain.com/invite/xyz67890
   ```

2. **查看会议信息**
   - 会议主题：Q1 产品规划会议
   - 发起人：李四
   - 协作模式：会议协作
   - 过期时间：2024-01-02 18:00

3. **快速注册**
   ```
   基本信息：
   - 用户名：wangwu
   - 邮箱：wangwu@example.com
   - 密码：******
   - 确认密码：******
   
   会议协作问卷：
   - 您对本次会议的期望是什么？
     了解产品规划，提供技术可行性意见
   
   - 您希望通过会议达成什么目标？
     确定技术实施方案
   ```

4. **登录进入会议**
   - 注册完成后自动重定向到登录页
   - 登录后进入会议协作界面

### 会议协作流程

#### 1. 本地 AI 对话

参与者通过本地 AI 助手表达观点：

```
参与者: "我认为应该优先开发移动端功能，因为 70% 的用户来自移动端"

AI: "好的，我已记录您的立场。是否需要补充更多依据？"

参与者: "补充：根据上个月的数据分析，移动端转化率是 PC 端的 2 倍"

AI: "已更新您的立场，现在上传到云端吗？"

参与者: "上传"
```

#### 2. 云端实时同步

```
云端大模型接收到立场：
- 参与者：王五
- 立场：优先开发移动端功能
- 依据：
  1. 70% 用户来自移动端
  2. 移动端转化率是 PC 端的 2 倍
- 时间：2024-01-02 10:35:20
```

#### 3. 立场分析和下发

```
云端分析：
- 检测到 3 位参与者支持移动端优先
- 2 位参与者支持 PC 端优先
- 识别到分歧点：资源分配

下发给所有参与者：
"当前立场分布：
✓ 移动端优先：3 票（王五、赵六、孙七）
✓ PC 端优先：2 票（李四、周八）

主要分歧：资源分配
建议讨论：是否可以分阶段实施？"
```

#### 4. 继续讨论

参与者收到云端分析后，继续通过本地 AI 对话：

```
参与者: "我同意分阶段实施，先做移动端核心功能，再完善 PC 端"

AI: "这是一个折中方案。我帮您整理提议：
1. 第一阶段（2 周）：移动端核心功能
2. 第二阶段（2 周）：PC 端基础功能
3. 第三阶段（持续）：两端功能对齐

是否提交这个提议？"

参与者: "是的，提交"
```

#### 5. 达成决策

```
云端检测到：
- 分阶段方案获得 4 票支持
- 1 票弃权

系统判断：达成共识 ✅

生成会议纪要：
- 决策：采用分阶段实施方案
- 时间线：第一阶段 1 月 3 日 - 1 月 17 日
- 负责人：待分配
- 后续行动：需要细化技术方案
```

---

## 管理邀请链接

### 查看邀请链接列表

```bash
# 获取某个事项的所有邀请链接
curl -X GET https://your-domain.com/api/invitations/matter/123 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

返回：
```json
[
  {
    "id": "uuid-1",
    "short_code": "abc12345",
    "invited_name": "张三",
    "expires_at": "2024-01-08T00:00:00Z",
    "max_uses": 10,
    "used_count": 3,
    "status": "active",
    "is_active": true
  },
  {
    "id": "uuid-2",
    "short_code": "xyz67890",
    "invited_name": null,
    "expires_at": "2024-01-02T00:00:00Z",
    "max_uses": null,
    "used_count": 15,
    "status": "expired",
    "is_active": false
  }
]
```

### 撤销邀请链接

```bash
curl -X POST https://your-domain.com/api/invitations/uuid-1/revoke \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**使用场景**：
- 邀请对象变更
- 发现安全问题
- 项目取消或推迟

### 验证邀请链接

```bash
# 检查邀请链接是否有效（无需登录）
curl -X GET https://your-domain.com/api/invitations/validate/abc12345
```

返回：
```json
{
  "valid": true,
  "matter_id": 123,
  "matter_title": "产品研发项目",
  "invited_name": "张三",
  "expires_at": "2024-01-08T00:00:00Z"
}
```

---

## 常见问题

### Q1: 邀请链接的有效期是多久？

**A**: 默认 3 天，创建时可以自定义（1-365 天）。会议邀请建议设置较短（1 天），项目邀请可以设置较长（7-30 天）。

### Q2: 一个邀请链接可以被多少人使用？

**A**: 创建时可以指定 `max_uses`：
- `max_uses: 1` - 只能使用一次（专属邀请）
- `max_uses: 10` - 最多 10 人使用
- `max_uses: null` - 无限制（公开邀请）

### Q3: 邀请链接可以撤销吗？

**A**: 可以。只有创建者或事项管理员可以撤销邀请链接。撤销后，该链接立即失效。

### Q4: 受邀者必须填写问卷吗？

**A**: 
- 项目协作：建议填写，帮助团队了解成员背景
- 会议协作：可选，但有助于更好的讨论
- 技术实现：问卷字段在模板中标记为可选

### Q5: 邀请链接被分享到了不该去的地方怎么办？

**A**: 立即撤销该链接，然后创建新的邀请链接。撤销后，旧链接无法再使用。

### Q6: 如何批量邀请多个人？

**A**: 
- **方案 1**: 创建一个 `max_uses: null` 的公开邀请链接，发送给所有人
- **方案 2**: 为每个人创建专属邀请链接（`max_uses: 1`，指定 `invited_name`）
- **未来功能**: 将支持批量导入邮箱列表自动生成邀请

### Q7: 会议协作和项目协作有什么区别？

**A**: 

| 特性 | 会议协作 | 项目协作 |
|------|---------|---------|
| 时效性 | 临时、短期 | 长期、持续 |
| 有效期 | 建议 1 天 | 建议 7-30 天 |
| 使用次数 | 无限制 | 有限制 |
| 问卷 | 简单期望 | 详细背景 |
| 实时性 | 强调实时同步 | 异步协作为主 |

### Q8: 如何跟踪邀请链接的使用情况？

**A**: 
```bash
# 查看邀请链接详情
curl -X GET https://your-domain.com/api/invitations/matter/123 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

返回的 `used_count` 字段显示已使用次数。

### Q9: 邀请链接是否安全？

**A**: 是的，系统提供多层安全保障：
- ✅ 8 位随机短码（避免混淆字符）
- ✅ 记录使用信息（IP、User-Agent、时间）
- ✅ 支持过期时间和使用次数限制
- ✅ 支持随时撤销
- ✅ 密码使用 bcrypt 哈希存储

### Q10: 受邀者注册后可以立即开始协作吗？

**A**: 
- 注册成功后会重定向到登录页
- 登录后自动进入相关事项
- 会议协作：可以立即开始实时讨论
- 项目协作：可以查看项目信息并开始工作

---

## 最佳实践

### 项目协作

1. **明确邀请对象**：使用 `invited_name` 字段
2. **设置合理有效期**：通常 7-14 天
3. **限制使用次数**：防止链接被滥用
4. **及时撤销**：项目取消或延期时立即撤销邀请

### 会议协作

1. **快速生成**：会议前 1-2 小时生成链接
2. **无限制使用**：`max_uses: null`，方便参与者加入
3. **短有效期**：会议结束后自动过期
4. **提前测试**：确保受邀者可以顺利注册和登录

### 安全建议

1. **不要公开分享**：通过私密渠道发送邀请链接
2. **定期清理**：撤销不再需要的邀请链接
3. **监控使用**：关注异常的使用模式
4. **及时更新**：发现问题立即撤销并重新创建

---

## 技术集成

### 通过 MCP 工具使用

```python
# 本地 AI 对话示例

用户: "为项目'产品研发'创建一个邀请链接"

AI: "好的，我需要一些信息：
1. 有效期（天数）？
2. 最多几人使用？
3. 受邀人姓名（可选）？"

用户: "7 天，10 人，张三"

AI: 调用 create_invitation_link(
    matter_id=123,
    expires_in_days=7,
    max_uses=10,
    invited_name="张三"
)

AI: "邀请链接已创建 ✅
- 链接：https://your-domain.com/invite/abc12345
- 有效期：2024-01-08
- 最多使用：10 次
- 受邀人：张三

是否需要我将链接发送给张三？"
```

### 通过 API 集成

```python
import requests

def create_project_invitation(matter_id, invited_name):
    """为项目创建邀请链接。"""
    response = requests.post(
        "https://your-domain.com/api/invitations/create",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "matter_id": matter_id,
            "expires_in_days": 7,
            "max_uses": 10,
            "invited_name": invited_name,
        }
    )
    return response.json()

# 使用
invitation = create_project_invitation(123, "张三")
print(f"邀请链接: {invitation['full_url']}")
```

---

## 下一步

- 📖 阅读 [API 文档](./api-documentation.md)
- 🔧 查看 [技术实现](./invitation-system-implementation.md)
- 💬 加入[社区讨论](https://github.com/your-repo/discussions)

如有问题，请联系技术支持或提交 Issue。
