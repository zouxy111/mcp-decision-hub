# 邀请链接系统开发进度

## 已完成

### Phase 1: 数据模型与业务逻辑 ✅
- [x] 数据库模型 (`InvitationLink`, `InvitationConsumption`)
- [x] 数据库迁移脚本（v13）
- [x] 业务逻辑函数（16 个测试，全部通过）
  - 生成短链码（6位，排除易混淆字符）
  - 创建邀请链接
  - 验证链接有效性
  - 消费链接（自动创建账号并加入事项）
  - 撤销链接
  - 清理过期链接

### Phase 2: API 端点 ✅
- [x] 路由文件 (`hub/web/routes_invitation.py`)
- [x] API 测试（3 个测试，全部通过）
- [x] 路由注册到主应用
- [x] 所有 19 个测试通过

**API 端点：**
- `POST /api/invitation-links/create` - 创建邀请链接
- `POST /api/invitation-links/validate` - 验证邀请链接
- `POST /api/invitation-links/consume` - 消费邀请链接（注册+加入）
- `GET /matters/{matter_id}/invitations` - 获取事项的所有邀请链接
- `POST /api/invitation-links/{link_id}/revoke` - 撤销邀请链接

## 进行中

### Phase 3: 前端页面 🚧
- [ ] 邀请链接管理页面（事项详情页）
  - [ ] 创建邀请链接表单
  - [ ] 邀请链接列表
  - [ ] 复制链接、撤销链接功能
- [ ] 邀请接受页面（公开访问）
  - [ ] 显示邀请信息
  - [ ] 注册表单
  - [ ] 引导问卷

## 待完成

### Phase 4: MCP 工具集成
- [ ] 创建 MCP 工具定义
- [ ] 本地 Agent 调用 API
- [ ] 云端配置同步

### Phase 5: 测试与文档
- [ ] 端到端测试
- [ ] 用户文档
- [ ] API 文档

## 测试状态

✅ **19/19 测试通过**
- Domain 层：16 个测试
- API 层：3 个测试
