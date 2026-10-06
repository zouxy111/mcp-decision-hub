# 快速开始 - 多人协作测试指南

## 🚀 启动服务器

```bash
# 1. 启动 MCP 决策中台服务器
cd /path/to/mcp-decision-hub
uv run uvicorn hub.main:app --host 0.0.0.0 --port 8000 --reload
```

服务器启动后，可以通过 http://localhost:8000 访问。

## 📝 测试流程

### 场景：张陈祎（领导）邀请李明加入项目协作

#### 步骤 1：张陈祎创建协作事项并生成邀请链接

```bash
# 创建一个测试脚本
cat > test_leader.py << 'EOF'
"""模拟张陈祎（领导）创建协作并生成邀请链接"""
import requests
import json

BASE_URL = "http://localhost:8000"

# 1. 张陈祎的账号信息（假设已经注册）
leader_data = {
    "username": "zhangchenyi",
    "email": "zhangchenyi@company.com",
    "password": "password123"
}

# 2. 登录获取令牌（如果需要）
# response = requests.post(f"{BASE_URL}/api/auth/login", json=leader_data)
# token = response.json()["access_token"]

# 3. 创建协作事项
matter_data = {
    "title": "AI产品功能规划",
    "description": "讨论新AI产品的核心功能和技术路线",
    "mode": "project",  # 项目协作模式
    "decision_model_id": None  # 领导不使用决策模型，依靠个人判断
}

# response = requests.post(
#     f"{BASE_URL}/api/matters",
#     json=matter_data,
#     headers={"Authorization": f"Bearer {token}"}
# )
# matter_id = response.json()["id"]

# 4. 生成邀请链接
invitation_data = {
    "matter_id": "mat_0394690448074645aa4582a94542742f",  # 示例 ID
    "expires_in_days": 7,
    "max_uses": None,  # 无限制使用
    "invited_name": "李明"
}

# response = requests.post(
#     f"{BASE_URL}/api/invitations/create",
#     json=invitation_data,
#     headers={"Authorization": f"Bearer {token}"}
# )
# invitation = response.json()

# 5. 测试验证邀请链接
short_code = "WiqqvS"  # 示例短链码
response = requests.get(f"{BASE_URL}/validate/{short_code}")
result = response.json()

print("=" * 60)
print("📧 邀请链接已生成！")
print("=" * 60)
print(f"🔗 邀请链接: http://localhost:8000/invite/{short_code}")
print(f"👤 受邀人: {result.get('invited_name', '未指定')}")
print(f"📋 事项: {result.get('matter_title', '未知')}")
print(f"⏰ 有效期至: {result.get('expires_at', '未知')}")
print(f"✅ 链接状态: {'有效' if result['valid'] else '无效'}")
print("=" * 60)
print(f"\n📤 请将以下链接发送给李明：")
print(f"   http://localhost:8000/invite/{short_code}")
print()
EOF

python test_leader.py
```

#### 步骤 2：李明点击邀请链接并加入协作

```bash
# 在另一台电脑或浏览器中打开
open http://localhost:8000/invite/WiqqvS

# 或者用 curl 模拟
curl http://localhost:8000/invite/WiqqvS
```

李明会看到一个注册页面，填写信息后自动：
1. 创建账号
2. 加入项目
3. 开始协作

#### 步骤 3：李明通过本地 Agent 提交意见

```bash
cat > test_member.py << 'EOF'
"""模拟李明通过本地 Agent 提交意见"""
import requests

BASE_URL = "http://localhost:8000"

# 1. 李明登录
member_data = {
    "username": "liming",  # 注册时填写的用户名
    "password": "password456"
}

# response = requests.post(f"{BASE_URL}/api/auth/login", json=member_data)
# token = response.json()["access_token"]

# 2. 李明的本地 Agent 收集需求
local_context = """
通过与李明的对话，我了解到：
1. 他希望产品能支持多语言
2. 需要离线模式
3. 建议采用渐进式发布策略
"""

# 3. 提交到云端
opinion_data = {
    "matter_id": "mat_0394690448074645aa4582a94542742f",
    "content": local_context,
    "stance": "支持，但有补充建议"
}

# response = requests.post(
#     f"{BASE_URL}/api/opinions",
#     json=opinion_data,
#     headers={"Authorization": f"Bearer {token}"}
# )

print("✅ 李明的意见已提交到云端")
print("💬 内容:", opinion_data["content"])
EOF

python test_member.py
```

#### 步骤 4：查看协作状态

```bash
# 查看所有参与者
curl http://localhost:8000/api/matters/mat_0394690448074645aa4582a94542742f/participants

# 查看所有意见
curl http://localhost:8000/api/matters/mat_0394690448074645aa4582a94542742f/opinions

# 查看协作进展
curl http://localhost:8000/api/matters/mat_0394690448074645aa4582a94542742f
```

## 🎯 测试要点

### 1. 邀请链接功能
- ✅ 生成唯一短链码
- ✅ 设置有效期和使用次数
- ✅ 指定受邀人姓名（可选）
- ✅ 验证链接状态

### 2. 自动注册流程
- ✅ 点击链接自动进入注册页面
- ✅ 填写基本信息（用户名、邮箱、密码）
- ✅ 注册成功后自动加入事项
- ✅ 记录邀请消费记录

### 3. 本地 Agent 交互
- ✅ 本地 Agent 询问用户需求
- ✅ 收集完整信息后提交云端
- ✅ 云端存储为意见（Opinion）
- ✅ 其他成员可以查看

### 4. 云端协作
- ✅ 实时查看所有参与者
- ✅ 查看所有意见和立场
- ✅ 支持项目协作和会议协作两种模式

## 🔧 API 端点

### 邀请相关
- `POST /api/invitations/create` - 创建邀请链接
- `GET /validate/{short_code}` - 验证邀请链接
- `GET /invite/{short_code}` - 邀请页面（HTML）
- `POST /invite/{short_code}/consume` - 使用邀请链接（注册）

### 事项相关
- `POST /api/matters` - 创建事项
- `GET /api/matters/{id}` - 获取事项详情
- `GET /api/matters/{id}/participants` - 获取参与者列表
- `GET /api/matters/{id}/opinions` - 获取意见列表

### 意见相关
- `POST /api/opinions` - 提交意见
- `PUT /api/opinions/{id}` - 更新意见
- `DELETE /api/opinions/{id}` - 删除意见

## 🎬 实际演示

### 当前测试环境
- 服务器已启动: http://localhost:8000
- 示例邀请链接: http://localhost:8000/invite/WiqqvS
- 事项 ID: mat_0394690448074645aa4582a94542742f
- 受邀人: 李明

你现在可以：
1. 在这台电脑上模拟张陈祎（领导）
2. 在另一台电脑上打开邀请链接模拟李明
3. 测试完整的邀请-注册-协作流程

## 📱 下一步

1. **测试邀请流程**: 将 `http://localhost:8000/invite/WiqqvS` 发送给另一台电脑
2. **完善 Agent 交互**: 开发本地 Agent 的对话收集功能
3. **实现实时通知**: 当有新意见提交时通知相关人员
4. **添加权限控制**: 只有创建者可以生成邀请链接

## 🐛 问题排查

```bash
# 检查服务器状态
curl http://localhost:8000/health

# 查看数据库
sqlite3 data/hub.db ".tables"

# 查看日志
tail -f logs/server.log

# 重置数据库（谨慎使用）
rm data/hub.db
uv run python -m hub.db.init
```
