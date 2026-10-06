# 邀请链接系统实现总结

## 概述

本文档总结了 MCP 决策中台的邀请链接系统实现，包括两种协作方案的设计与实现。

## 系统架构

### 1. 数据模型

#### InvitationLink 表
- `id`: UUID 主键
- `short_code`: 短码（8位随机字符串）
- `matter_id`: 关联的事项 ID
- `created_by`: 创建者 ID
- `expires_at`: 过期时间
- `max_uses`: 最大使用次数（NULL = 无限制）
- `used_count`: 已使用次数
- `status`: 状态（active/expired/exhausted/revoked）
- `invited_name`: 受邀人姓名（可选）
- `created_at`: 创建时间
- `revoked_at`: 撤销时间
- `revoked_by`: 撤销者 ID

#### Matter 表扩展
- `mode`: 协作模式（meeting/project）
- 支持会议协作和项目协作两种模式

### 2. 核心功能模块

#### domain/invitation_links.py
业务逻辑层，提供以下核心功能：
- `create_invitation_link()`: 创建邀请链接
- `validate_invitation_link()`: 验证邀请链接有效性
- `consume_invitation_link()`: 消费邀请链接（注册用户并加入事项）
- `revoke_invitation()`: 撤销邀请链接
- `get_matter_invitations()`: 获取事项的所有邀请链接
- `cleanup_expired_invitations()`: 清理过期的邀请链接

#### web/routes_invitation.py
Web 路由层，提供：
- HTML 页面路由
  - `GET /invite/{short_code}`: 显示邀请接受页面
  - `POST /invite/{short_code}/accept`: 处理接受邀请表单
- API 端点
  - `POST /api/invitations/create`: 创建邀请链接
  - `GET /api/invitations/validate/{short_code}`: 验证邀请链接
  - `POST /api/invitations/consume/{short_code}`: 消费邀请链接
  - `GET /api/invitations/matter/{matter_id}`: 获取事项的所有邀请链接
  - `POST /api/invitations/{invitation_id}/revoke`: 撤销邀请链接

#### web/templates/invitation_accept.html
邀请接受页面模板，包含：
- 邀请信息展示
- 用户注册表单
- 协作模式特定的问卷（会议协作/项目协作）
- 错误提示

## 协作方案实现

### 方案 1：项目协作（Project Collaboration）

#### 特点
- 长期协作
- 明确的角色和权限
- 需要详细的用户信息

#### 流程
1. 领导创建项目事项
2. 通过 `/api/invitations/create` 生成邀请链接，指定 `mode=project`
3. 成员点击邀请链接进入注册页面
4. 填写注册信息：
   - 用户名
   - 邮箱
   - 密码
   - 姓名
   - 个人背景
   - 期望和需求
5. 提交后：
   - 创建用户账号
   - 自动加入项目事项
   - 记录使用信息
   - 重定向到登录页

#### 问卷内容（项目协作）
```
- 您的专业背景是什么？
- 您在项目中期望承担什么角色？
- 您对项目的期望是什么？
```

### 方案 2：会议协作（Meeting Collaboration）

#### 特点
- 临时性协作
- 快速接入
- 实时立场和内容下发

#### 流程
1. 领导创建会议事项
2. 通过 `/api/invitations/create` 生成邀请链接，指定 `mode=meeting`
3. 参与者点击邀请链接进入注册页面
4. 填写注册信息：
   - 用户名
   - 邮箱
   - 密码
   - 姓名
   - 会议期望
5. 提交后：
   - 创建用户账号
   - 自动加入会议事项
   - 记录使用信息
   - 重定向到登录页
6. 登录后进入会议协作界面
7. 通过本地 AI 对话，内容上传云端
8. 云端大模型实时处理立场和决策

#### 问卷内容（会议协作）
```
- 您对本次会议的期望是什么？
- 您希望通过会议达成什么目标？
```

## 安全特性

### 1. 邀请链接安全
- 使用 8 位随机字符串作为短码
- 支持设置过期时间（默认 3 天）
- 支持限制使用次数
- 支持撤销功能
- 记录使用信息（IP、User-Agent、使用时间）

### 2. 用户注册安全
- 密码哈希存储（bcrypt）
- 邮箱格式验证
- 用户名唯一性检查
- 密码确认验证

### 3. 权限控制
- 只有事项创建者可以生成邀请链接
- 只有创建者或事项管理员可以撤销邀请链接
- 邀请链接验证在消费时再次执行

## 技术实现亮点

### 1. 短码生成算法
```python
def _generate_short_code() -> str:
    """生成 8 位短码（数字+字母，避免混淆字符）。"""
    chars = "23456789abcdefghjkmnpqrstuvwxyz"  # 排除 0,1,i,l,o
    return "".join(secrets.choice(chars) for _ in range(8))
```

### 2. 状态管理
```python
@property
def is_active(self) -> bool:
    """邀请链接是否处于活跃状态。"""
    if self.status != "active":
        return False
    if self.expires_at and self.expires_at < datetime.now(timezone.utc):
        return False
    if self.max_uses and self.used_count >= self.max_uses:
        return False
    return True
```

### 3. 原子性操作
使用数据库事务确保邀请链接消费的原子性：
- 验证邀请链接
- 创建用户
- 创建参与者记录
- 记录使用信息
- 更新使用次数

### 4. 错误处理
定义了专门的异常类：
- `InvitationNotFoundError`: 邀请链接不存在
- `InvitationExpiredError`: 邀请链接已过期
- `InvitationExhaustedError`: 邀请链接已用尽
- `InvitationRevokedError`: 邀请链接已撤销

## 测试覆盖

### 单元测试（domain 层）
- 短码生成
- 邀请链接创建
- 有效性验证
- 消费逻辑
- 撤销功能
- 过期清理
- 多次使用场景

### API 测试
- 创建邀请链接
- 验证邀请链接
- 消费邀请链接
- 获取事项邀请列表
- 撤销邀请链接
- 权限控制

### Web 页面测试
- 显示有效邀请页面
- 显示无效邀请页面
- 成功接受邀请
- 密码不匹配处理
- 无效邀请码处理

**测试统计**: 31 个测试全部通过 ✅

## 使用示例

### 1. 创建邀请链接（项目协作）

```python
# API 调用
POST /api/invitations/create
{
    "matter_id": 123,
    "expires_in_days": 7,
    "max_uses": 10,
    "invited_name": "张三"
}

# 返回
{
    "id": "uuid",
    "short_code": "abc12345",
    "full_url": "/invite/abc12345",
    "expires_at": "2024-01-01T00:00:00Z",
    ...
}
```

### 2. 接受邀请

用户访问 `/invite/abc12345`，填写表单：
- 用户名: zhangsan
- 邮箱: zhangsan@example.com
- 密码: ******
- 确认密码: ******
- 背景: 软件工程师
- 期望: 参与技术决策

提交后自动创建账号并加入项目。

### 3. 创建会议邀请链接

```python
# 在创建 Matter 时指定 mode
matter = Matter(
    title="产品规划会议",
    goal="确定 Q1 产品路线",
    initiator_id=user.id,
    mode="meeting",  # 会议模式
    status="draft",
)

# 创建邀请链接
POST /api/invitations/create
{
    "matter_id": 123,
    "expires_in_days": 1,  # 会议链接通常有效期较短
    "max_uses": null,      # 无限制，方便多人加入
}
```

## 未来扩展

### 1. 实时会议功能
- WebSocket 支持实时立场同步
- 会议室状态管理
- 发言队列管理

### 2. 高级权限管理
- 角色预设（观察者、参与者、决策者）
- 动态权限调整

### 3. 邀请链接增强
- 批量邀请
- 邀请链接模板
- 自定义邀请消息

### 4. 分析和报告
- 邀请链接使用统计
- 参与者活跃度分析
- 协作效果评估

## 总结

邀请链接系统已完整实现，支持两种协作模式：

1. **项目协作**：适合长期项目，需要详细的用户信息和背景
2. **会议协作**：适合临时会议，快速接入，实时协作

系统具备完善的安全机制、错误处理和测试覆盖，可以安全地投入使用。通过简单的邀请链接，新用户可以快速注册并加入协作事项，大大降低了协作门槛。
