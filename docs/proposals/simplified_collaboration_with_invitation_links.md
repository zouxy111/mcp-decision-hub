# 领导简化协作方案：一键邀请 + 本地 Agent 自动配置

**场景**: 领导使用本地 Agent（仅日常记忆）快速发起协作，无需复杂操作  
**日期**: 2026-10-03  
**状态**: 需求分析与设计方案

---

## 一、核心需求分析 🎯

### 1.1 当前痛点

**领导端**:
- ❌ 没有决策模型和知识库（只有 Agent 的日常简单记忆）
- ❌ 需要登录 Web 界面创建事项
- ❌ 手动输入参与人信息
- ❌ 操作步骤繁琐（创建事项 → 邀请人员 → 配置选项）

**参与者端**:
- ❌ 需要先注册账号
- ❌ 手动加入事项
- ❌ 不知道如何开始

### 1.2 理想流程

```
领导在本地 Agent 说：
"我想召开一个关于 Q1 战略的会议，邀请 Alice、Bob、Carol"

    ↓

Agent 自动：
1. 与领导对话，收集信息（会议目的、背景）
2. 提交到云端，创建事项
3. 云端生成邀请链接
4. Agent 获取链接，发送给参与者

    ↓

参与者点击链接：
1. 在本地 Agent 中打开
2. Agent 引导：输入姓名、选择协作类型
3. 自动注册 + 绑定 + 加入事项
4. 开始协作

    ↓

多人协作：
- 实时立场同步
- 云端 AI 生成摘要
- 本地 Agent 展示给每个人
- 循环收敛，达成共识
```

---

## 二、架构设计 🏗️

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    本地 Agent 层                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  领导 Agent                参与者 Agent                  │
│      ↓                          ↓                        │
│  [对话引导]              [链接自动绑定]                   │
│  [自动配置]              [自动注册]                       │
│      ↓                          ↓                        │
│  ┌────────────────────────────────────┐                 │
│  │     MCP 协议（新增工具）            │                 │
│  │                                    │                 │
│  │  • create_collaborative_matter    │                 │
│  │  • generate_invitation_link       │                 │
│  │  • consume_invitation_link        │                 │
│  │  • auto_configure_agent           │                 │
│  └────────────────────────────────────┘                 │
│                      ↓                                   │
└──────────────────────┼───────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│                   云端服务器                              │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  [邀请链接服务]                                          │
│  • 生成唯一短链                                          │
│  • 记录邀请上下文（事项ID、邀请人、角色）                 │
│  • 跟踪链接状态（未使用/已使用/已过期）                   │
│                                                          │
│  [自动注册服务]                                          │
│  • 基于链接创建临时账号                                   │
│  • 绑定本地 Agent Token                                  │
│  • 自动加入对应事项                                       │
│                                                          │
│  [协作模式]                                              │
│  • 会议模式：实时同步立场                                 │
│  • 项目模式：异步提交观点                                 │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 2.2 核心组件

#### 组件 1：邀请链接生成器

```python
# hub/api/invitations.py

@dataclass
class InvitationLink:
    link_id: str              # 短链 ID（6-8位）
    matter_id: str            # 关联的事项
    inviter_id: int           # 邀请人
    role: str                 # participant | viewer
    mode: str                 # meeting | project
    expires_at: datetime      # 过期时间
    max_uses: int            # 最大使用次数（默认1）
    used_count: int          # 已使用次数
    status: str              # active | expired | revoked

def generate_invitation_link(
    session: Session,
    *,
    matter_id: str,
    inviter: User,
    role: str = "participant",
    ttl_hours: int = 72,
    max_uses: int = 1
) -> tuple[str, str]:
    """
    生成邀请链接
    
    Returns:
        (link_id, full_url)
        例如: ("xY9kL2", "https://hub.tdp-demo.work/join/xY9kL2")
    """
    link_id = generate_short_id()  # 生成6位短码
    
    invitation = InvitationLink(
        link_id=link_id,
        matter_id=matter_id,
        inviter_id=inviter.id,
        role=role,
        mode=get_matter_mode(session, matter_id),
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        max_uses=max_uses,
        used_count=0,
        status="active"
    )
    
    session.add(invitation)
    session.flush()
    
    full_url = f"{settings.base_url}/join/{link_id}"
    return link_id, full_url
```

#### 组件 2：引导式问卷（本地 Agent）

```python
# 本地 Agent 逻辑（伪代码）

class CollaborationWizard:
    """协作引导向导"""
    
    async def guide_leader_create(self, initial_request: str):
        """引导领导创建协作事项"""
        
        # Step 1: 提取意图和参与者
        parsed = await self.parse_intent(initial_request)
        # 例如: {
        #   "type": "meeting",
        #   "topic": "Q1战略",
        #   "participants": ["Alice", "Bob", "Carol"]
        # }
        
        # Step 2: 引导式对话收集完整信息
        context = await self.collect_details({
            "title": f"{parsed['topic']} 协作",
            "goal": None,  # 待询问
            "background": None,  # 待询问
            "mode": parsed["type"],  # meeting | project
            "participants": parsed["participants"]
        })
        
        # 对话示例：
        # Agent: "好的，我来帮您发起关于「Q1战略」的会议协作。
        #        请问这次会议的主要目标是什么？"
        # 领导: "确定Q1的三大战略重点"
        # 
        # Agent: "明白了。还有什么背景信息需要让参与者了解吗？"
        # 领导: "市场环境变化很快，我们需要快速决策"
        #
        # Agent: "好的。您提到要邀请 Alice、Bob、Carol，
        #        请问还需要其他人参与吗？"
        # 领导: "不用了，就这三个人"
        
        # Step 3: 确认信息
        await self.confirm(context)
        # Agent: "请确认信息：
        #        • 主题：Q1战略协作
        #        • 目标：确定Q1的三大战略重点
        #        • 参与者：Alice、Bob、Carol（3人）
        #        • 类型：会议模式（实时协作）
        #        确认创建吗？"
        
        # Step 4: 调用 MCP 工具创建
        result = await mcp_client.call_tool(
            "create_collaborative_matter",
            {
                "title": context["title"],
                "goal": context["goal"],
                "background": context["background"],
                "mode": context["mode"],
                "participant_names": context["participants"]
            }
        )
        
        # Step 5: 获取邀请链接
        links = result["invitation_links"]
        # {
        #   "Alice": "https://hub.tdp-demo.work/join/xY9kL2",
        #   "Bob": "https://hub.tdp-demo.work/join/aB3mN7",
        #   "Carol": "https://hub.tdp-demo.work/join/pQ8rT4"
        # }
        
        # Step 6: 展示结果并引导分享
        await self.show_result(result, links)
        # Agent: "✅ 协作事项已创建！
        #        
        #        邀请链接已生成，请将以下链接发送给对应的参与者：
        #        
        #        • Alice: https://hub.tdp-demo.work/join/xY9kL2
        #        • Bob: https://hub.tdp-demo.work/join/aB3mN7
        #        • Carol: https://hub.tdp-demo.work/join/pQ8rT4
        #        
        #        参与者点击链接后会自动加入协作。
        #        现在您可以开始提交您的立场了。"
        
        return result
    
    async def guide_participant_join(self, invitation_url: str):
        """引导参与者通过链接加入"""
        
        # Step 1: 解析链接，获取邀请信息
        link_info = await mcp_client.call_tool(
            "get_invitation_info",
            {"invitation_url": invitation_url}
        )
        # 返回: {
        #   "matter_title": "Q1战略协作",
        #   "inviter_name": "张总",
        #   "mode": "meeting",
        #   "participant_count": 3
        # }
        
        # Step 2: 引导式问卷
        await self.show_welcome(link_info)
        # Agent: "👋 欢迎！
        #        
        #        张总邀请您参加「Q1战略协作」会议。
        #        在加入之前，请告诉我一些基本信息。"
        
        # 问题1: 姓名
        name = await self.ask("请问您的姓名是？")
        # 参与者: "Alice"
        
        # 问题2: 确认协作类型（如果不明确）
        if link_info["mode"] == "unknown":
            mode = await self.ask_mode()
            # Agent: "这次协作是：
            #        1. 会议协作（实时讨论，快速决策）
            #        2. 项目协作（异步提交，深度思考）
            #        请选择："
        else:
            mode = link_info["mode"]
            await self.confirm_mode(mode)
            # Agent: "这是一个会议协作，将实时同步各方立场。"
        
        # 问题3: 简单背景（可选）
        background = await self.ask_optional(
            "您对这个议题有什么初步想法吗？（可跳过）"
        )
        
        # Step 3: 调用 MCP 自动注册 + 加入
        result = await mcp_client.call_tool(
            "consume_invitation_link",
            {
                "invitation_url": invitation_url,
                "user_name": name,
                "initial_notes": background
            }
        )
        # 云端自动：
        # 1. 创建账号（username: alice_xY9kL2）
        # 2. 生成 MCP Token
        # 3. 绑定到本地 Agent
        # 4. 加入对应事项
        # 5. 返回事项详情
        
        # Step 4: 保存配置到本地
        await self.save_agent_config({
            "mcp_token": result["mcp_token"],
            "user_id": result["user_id"],
            "user_name": name,
            "matter_id": result["matter_id"]
        })
        
        # Step 5: 展示欢迎和任务
        await self.show_onboarding(result)
        # Agent: "✅ 欢迎加入，Alice！
        #        
        #        协作已准备就绪：
        #        • 主题：Q1战略协作
        #        • 参与者：您、Bob、Carol（已加入2/3）
        #        • 当前状态：等待所有人加入
        #        
        #        您可以先思考一下这个议题，
        #        等所有人就位后会议就会开始。"
        
        return result
```

#### 组件 3：MCP 工具扩展

```python
# hub/mcp_server/tools.py（新增工具）

@mcp.tool
def create_collaborative_matter(
    title: str,
    goal: str,
    participant_names: list[str],
    background: str = "",
    mode: str = "meeting",  # meeting | project
    max_rounds: int = 6,
    timeout_minutes: int = 60
) -> dict:
    """
    创建协作事项并生成邀请链接（简化版 create_matter）
    
    领导通过本地 Agent 发起协作的唯一入口。
    自动生成每个参与者的专属邀请链接。
    
    Args:
        title: 事项标题
        goal: 协作目标
        participant_names: 参与者姓名列表（昵称或真名）
        background: 背景说明
        mode: meeting（会议模式-实时） | project（项目模式-异步）
        max_rounds: 最大轮次
        timeout_minutes: 单轮超时时间
    
    Returns:
        {
            "matter_id": "mat_xxx",
            "title": "Q1战略协作",
            "status": "draft",
            "mode": "meeting",
            "invitation_links": {
                "Alice": "https://hub.tdp-demo.work/join/xY9kL2",
                "Bob": "https://hub.tdp-demo.work/join/aB3mN7",
                "Carol": "https://hub.tdp-demo.work/join/pQ8rT4"
            },
            "created_at": "2026-10-03T10:30:00Z"
        }
    """
    return _call(
        session_factory, methods.mcp_create_collaborative_matter,
        settings=settings,
        user_id=_current_user_id(),
        payload={
            "title": title,
            "goal": goal,
            "participant_names": participant_names,
            "background": background,
            "mode": mode,
            "max_rounds": max_rounds,
            "timeout_minutes": timeout_minutes
        }
    )

@mcp.tool
def get_invitation_info(invitation_url: str) -> dict:
    """
    查询邀请链接信息（不消费链接）
    
    用于参与者点击链接后预览协作内容。
    
    Returns:
        {
            "matter_id": "mat_xxx",
            "matter_title": "Q1战略协作",
            "inviter_name": "张总",
            "mode": "meeting",
            "participant_count": 3,
            "joined_count": 1,
            "status": "active",
            "expires_at": "2026-10-06T10:30:00Z"
        }
    """
    link_id = extract_link_id(invitation_url)
    return _call(
        session_factory, methods.mcp_get_invitation_info,
        settings=settings,
        link_id=link_id
    )

@mcp.tool
def consume_invitation_link(
    invitation_url: str,
    user_name: str,
    initial_notes: str = ""
) -> dict:
    """
    消费邀请链接：自动注册 + 绑定 + 加入事项
    
    参与者通过本地 Agent 加入协作的唯一入口。
    云端自动完成账号创建、Token 生成、事项加入。
    
    Args:
        invitation_url: 邀请链接
        user_name: 参与者姓名
        initial_notes: 初步想法（可选）
    
    Returns:
        {
            "user_id": 123,
            "user_name": "Alice",
            "mcp_token": "mcp_xxx...xxx",  # 自动生成的 MCP Token
            "matter_id": "mat_xxx",
            "matter_title": "Q1战略协作",
            "role": "participant",
            "status": "active",
            "next_steps": "等待所有参与者加入后开始协作"
        }
    """
    link_id = extract_link_id(invitation_url)
    return _call(
        session_factory, methods.mcp_consume_invitation_link,
        settings=settings,
        link_id=link_id,
        user_name=user_name,
        initial_notes=initial_notes
    )

@mcp.tool
def start_collaborative_matter(matter_id: str) -> dict:
    """
    启动协作事项（简化版 start_matter）
    
    领导在确认所有人加入后，通过本地 Agent 一键启动。
    
    Returns:
        {
            "matter_id": "mat_xxx",
            "status": "collecting",
            "round_number": 1,
            "participants": [
                {"name": "Alice", "status": "active"},
                {"name": "Bob", "status": "active"},
                {"name": "Carol", "status": "active"}
            ],
            "message": "协作已启动，请各位提交立场"
        }
    """
    return _call(
        session_factory, methods.mcp_start_collaborative_matter,
        settings=settings,
        user_id=_current_user_id(),
        matter_id=matter_id
    )

@mcp.tool
def get_my_collaborative_matters(
    status: str | None = None,
    limit: int = 20
) -> dict:
    """
    获取我参与的所有协作事项
    
    用于本地 Agent 展示协作列表。
    
    Args:
        status: draft | collecting | in_progress | awaiting_decision | done
        limit: 最多返回数量
    
    Returns:
        {
            "matters": [
                {
                    "matter_id": "mat_xxx",
                    "title": "Q1战略协作",
                    "status": "collecting",
                    "mode": "meeting",
                    "my_role": "participant",
                    "round_number": 1,
                    "pending_tasks": 1,
                    "created_at": "2026-10-03T10:30:00Z"
                }
            ],
            "total": 1
        }
    """
    return _call(
        session_factory, methods.mcp_get_my_collaborative_matters,
        settings=settings,
        user_id=_current_user_id(),
        status=status,
        limit=limit
    )
```

---

## 三、协作模式对比 🔄

### 3.1 会议模式（Meeting Mode）

**特点**: 实时同步，快速收敛

```
时间轴：
10:00  所有人加入，会议启动
10:05  Alice 提交立场 → 云端立即处理
10:06  Bob 看到 Alice 的立场摘要
10:08  Bob 提交立场 → 云端立即处理
10:09  Alice、Carol 看到 Bob 的立场摘要
10:12  Carol 提交立场 → 云端立即处理
10:13  所有人看到完整摘要（共识/分歧/追问）
10:15  第二轮开始...
```

**适用场景**:
- ✅ 战略决策会
- ✅ 紧急事项讨论
- ✅ 快速拍板需求

**技术实现**:
- 立场提交后立即触发 LLM 处理（不等批处理窗口）
- 增量摘要：每增加一个人的立场，更新一次摘要
- 实时通知：通过 MCP 状态变化通知所有 Agent

### 3.2 项目模式（Project Mode）

**特点**: 异步协作，深度思考

```
时间轴：
Day 1  所有人加入，项目启动
Day 2  Alice 提交立场
Day 3  Bob 提交立场
Day 5  Carol 提交立场
Day 5  批处理窗口结束 → 云端处理
Day 5  所有人看到摘要
Day 7  第二轮开始...
```

**适用场景**:
- ✅ 长期项目规划
- ✅ 跨时区团队协作
- ✅ 需要深度调研的决策

**技术实现**:
- 使用批处理机制（上一个方案）
- 时间窗口：24-48小时
- 异步通知：定期检查，不急于实时

---

## 四、用户体验流程 📱

### 4.1 领导发起流程

```
步骤 1: 自然语言发起
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
领导: "我想召开一个关于Q1战略的会议，
      邀请 Alice、Bob、Carol"

Agent: "好的，我来帮您发起。
       请问这次会议的主要目标是什么？"

步骤 2: 引导式对话
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
领导: "确定Q1的三大战略重点"

Agent: "明白了。还有什么背景信息需要
       让参与者了解吗？"

领导: "市场变化很快，需要快速决策"

Agent: "好的。这是一个需要快速决策的会议，
       我建议使用「会议模式」，可以实时看到
       大家的立场。您同意吗？"

步骤 3: 确认创建
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "请确认信息：
       
       📋 主题：Q1战略协作
       🎯 目标：确定Q1的三大战略重点
       💬 背景：市场变化快，需快速决策
       👥 参与者：Alice、Bob、Carol（3人）
       ⚡ 模式：会议模式（实时协作）
       
       确认创建吗？"

领导: "确认"

步骤 4: 自动创建并生成链接
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "✅ 协作事项已创建！
       
       邀请链接已生成（有效期3天）：
       
       Alice 👉 https://hub.tdp-demo.work/join/xY9kL2
       Bob   👉 https://hub.tdp-demo.work/join/aB3mN7
       Carol 👉 https://hub.tdp-demo.work/join/pQ8rT4
       
       💡 提示：
       - 将链接发送给对应的人
       - 他们点击后会自动加入
       - 等所有人就位后会议自动开始
       
       现在您可以先思考您的立场，
       或者等所有人加入后再开始。"
```

### 4.2 参与者加入流程

```
步骤 1: 点击链接
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Alice 点击链接 https://hub.tdp-demo.work/join/xY9kL2]

[本地 Agent 自动打开]

步骤 2: 欢迎引导
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "👋 欢迎！
       
       张总邀请您参加「Q1战略协作」会议。
       
       📋 主题：Q1战略协作
       🎯 目标：确定Q1的三大战略重点
       💬 背景：市场变化快，需快速决策
       👥 参与者：3人
       ⚡ 模式：会议模式（实时协作）
       
       在加入之前，请告诉我一些基本信息。"

步骤 3: 简短问卷
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "请问您的姓名是？"
Alice: "Alice"

Agent: "好的，Alice。
       您对这个议题有什么初步想法吗？
       （可以跳过，之后再详细讨论）"
Alice: "我觉得应该重点关注AI业务"

步骤 4: 自动配置
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "正在为您配置云端连接..."

[云端自动完成：]
- 创建账号 alice_xY9kL2
- 生成 MCP Token
- 绑定到本地 Agent
- 加入协作事项

步骤 5: 完成加入
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Agent: "✅ 欢迎加入，Alice！
       
       🎯 协作事项：Q1战略协作
       👥 参与者：您、Bob、Carol
       ✅ 已加入：2/3（您和Bob）
       ⏳ 等待：Carol
       
       💡 下一步：
       - 您可以先思考您的立场
       - 等 Carol 加入后会议就会开始
       - 我会提醒您何时可以提交观点
       
       如果您现在有想法，也可以随时告诉我，
       我会帮您记录下来。"
```

### 4.3 协作进行流程（会议模式）

```
所有人加入后
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Alice Agent]
Agent: "✅ 所有参与者已就位！
       
       会议现在开始，请提交您的立场。
       
       💬 您之前说：「应该重点关注AI业务」
       需要我帮您展开并提交吗？"

Alice: "好的，帮我展开一下"

Agent: "根据您的想法，我整理如下：
       
       【立场】：优先发展AI业务
       【理由】：
       1. 市场趋势明确，AI是未来方向
       2. 竞争对手已经布局，我们需要跟进
       3. 有技术积累，可以快速启动
       
       【建议】：
       - Q1启动AI项目组
       - 预算分配向AI倾斜
       - 招聘AI人才
       
       您觉得这样表述准确吗？"

Alice: "很好，提交吧"

Agent: "✅ 立场已提交到云端"

第一轮进行中
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[2分钟后]

[Alice Agent]
Agent: "💬 Bob 刚刚提交了立场
       
       【Bob的观点摘要】：
       - 同意AI重要，但建议稳健推进
       - 建议先做市场调研和可行性分析
       - 担心资源分散影响现有业务
       
       您想回应 Bob 的观点吗？"

[3分钟后]

[Alice Agent]
Agent: "💬 Carol 刚刚提交了立场
       
       【Carol的观点摘要】：
       - 支持AI业务，但强调人才培养
       - 建议内部培训 + 外部招聘并行
       - 提出预算需要财务部门评估
       
       ━━━━━━━━━━━━━━━━━━━━━━━━━
       
       📊 云端AI摘要（第1轮）：
       
       ✅ 共识点：
       1. 全体认同AI业务是战略重点
       2. 都认为需要投入资源
       
       ⚠️ 分歧点：
       1. 推进节奏：激进 vs 稳健
       2. 资源分配：重点突破 vs 平衡发展
       
       ❓ 待讨论：
       1. 预算上限是多少？
       2. 团队能力现状如何？
       3. 市场窗口期有多长？
       
       🔄 收敛度：中等
       建议：进入第二轮，聚焦分歧点讨论
       
       ━━━━━━━━━━━━━━━━━━━━━━━━━
       
       💡 第二轮开始：
       请针对以上分歧点和待讨论问题，
       提交您的进一步观点。"
```

---

## 五、技术实现细节 ⚙️

### 5.1 数据库表结构（新增）

```sql
-- 邀请链接表
CREATE TABLE invitation_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id VARCHAR(8) NOT NULL UNIQUE,      -- 短链ID
    matter_id VARCHAR(26) NOT NULL,          -- 关联事项
    inviter_id INTEGER NOT NULL,             -- 邀请人
    invited_name VARCHAR(100),               -- 预期姓名（可选）
    role VARCHAR(20) NOT NULL DEFAULT 'participant',
    mode VARCHAR(20) NOT NULL,               -- meeting | project
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (matter_id) REFERENCES matters(id),
    FOREIGN KEY (inviter_id) REFERENCES users(id)
);

-- 邀请链接使用记录
CREATE TABLE invitation_consumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id VARCHAR(8) NOT NULL,
    user_id INTEGER NOT NULL,                -- 消费后创建的用户
    user_name VARCHAR(100) NOT NULL,
    consumed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (link_id) REFERENCES invitation_links(link_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Matter 新增字段
ALTER TABLE matters ADD COLUMN mode VARCHAR(20) DEFAULT 'project';
  -- meeting: 会议模式（实时）
  -- project: 项目模式（异步）

ALTER TABLE matters ADD COLUMN auto_start BOOLEAN DEFAULT FALSE;
  -- TRUE: 所有人加入后自动启动
  -- FALSE: 需要发起人手动启动
```

### 5.2 短链生成算法

```python
import secrets
import string

def generate_short_id(length: int = 6) -> str:
    """
    生成短链ID
    
    使用字符：大小写字母 + 数字（去除易混淆字符）
    排除：0/O, 1/I/l
    
    6位 = 56^6 ≈ 30 billion 种组合
    """
    alphabet = string.ascii_letters + string.digits
    # 排除易混淆字符
    alphabet = alphabet.replace('0', '').replace('O', '')
    alphabet = alphabet.replace('1', '').replace('I', '').replace('l', '')
    
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def generate_unique_link_id(session: Session) -> str:
    """生成唯一的链接ID（处理碰撞）"""
    max_attempts = 10
    for _ in range(max_attempts):
        link_id = generate_short_id()
        existing = session.scalar(
            select(InvitationLink).where(InvitationLink.link_id == link_id)
        )
        if existing is None:
            return link_id
    raise RuntimeError("无法生成唯一链接ID（连续碰撞）")
```

### 5.3 自动注册逻辑

```python
def auto_register_from_invitation(
    session: Session,
    *,
    link_id: str,
    user_name: str,
    initial_notes: str = ""
) -> dict:
    """
    通过邀请链接自动注册并加入事项
    
    完成：
    1. 验证链接有效性
    2. 创建用户账号
    3. 生成 MCP Token
    4. 加入对应事项
    5. 记录消费记录
    """
    # 1. 验证链接
    link = session.scalar(
        select(InvitationLink).where(InvitationLink.link_id == link_id)
    )
    if link is None:
        raise ApiError(404, "INVITATION_NOT_FOUND", "邀请链接不存在")
    
    if link.status != "active":
        raise ApiError(400, "INVITATION_INVALID", 
                      f"邀请链接状态异常: {link.status}")
    
    if link.expires_at < utcnow():
        link.status = "expired"
        session.commit()
        raise ApiError(400, "INVITATION_EXPIRED", "邀请链接已过期")
    
    if link.used_count >= link.max_uses:
        link.status = "exhausted"
        session.commit()
        raise ApiError(400, "INVITATION_EXHAUSTED", 
                      "邀请链接使用次数已达上限")
    
    # 2. 创建用户账号
    username = f"{slugify(user_name)}_{link_id}"  # 例如: alice_xY9kL2
    password = generate_random_password()  # 生成随机密码（用户不需要知道）
    
    user = User(
        username=username,
        display_name=user_name,
        role="member",
        is_active=True,
        created_via="invitation",
        invitation_link_id=link_id
    )
    user.password_hash = hash_password(password)
    session.add(user)
    session.flush()
    
    # 3. 生成 MCP Token（自动绑定到本地 Agent）
    token_str = generate_mcp_token()
    token = MCP_Token(
        user_id=user.id,
        token=token_str,
        name=f"auto_{user_name}",
        created_at=utcnow()
    )
    session.add(token)
    session.flush()
    
    # 4. 加入事项
    participant = MatterParticipant(
        matter_id=link.matter_id,
        user_id=user.id,
        role=link.role,
        joined_via="invitation",
        initial_notes=initial_notes
    )
    session.add(participant)
    
    # 5. 更新链接使用记录
    link.used_count += 1
    if link.used_count >= link.max_uses:
        link.status = "exhausted"
    
    consumption = InvitationConsumption(
        link_id=link_id,
        user_id=user.id,
        user_name=user_name,
        consumed_at=utcnow()
    )
    session.add(consumption)
    
    # 6. 审计日志
    audit.record_audit(
        session, audit.USER_JOINED_VIA_INVITATION,
        actor_user_id=user.id,
        matter_id=link.matter_id,
        detail={
            "link_id": link_id,
            "inviter_id": link.inviter_id,
            "user_name": user_name
        }
    )
    
    session.commit()
    
    # 7. 检查是否所有人已加入（自动启动）
    matter = session.get(Matter, link.matter_id)
    if matter.auto_start:
        check_and_auto_start(session, matter)
    
    return {
        "user_id": user.id,
        "username": username,
        "user_name": user_name,
        "mcp_token": token_str,
        "matter_id": link.matter_id,
        "matter_title": matter.title,
        "role": link.role,
        "status": "active"
    }
```

---

## 六、实施路线图 📅

### Phase 1：邀请链接基础（2周）

**Week 1**:
- [ ] 设计数据库表结构
- [ ] 实现短链生成算法
- [ ] 实现链接有效性验证
- [ ] 单元测试

**Week 2**:
- [ ] 实现自动注册逻辑
- [ ] MCP 工具：`create_collaborative_matter`
- [ ] MCP 工具：`consume_invitation_link`
- [ ] 集成测试

### Phase 2：引导式问卷（2周）

**Week 3**:
- [ ] 设计对话流程脚本
- [ ] 实现领导端引导逻辑
- [ ] 实现参与者端引导逻辑
- [ ] 本地 Agent 集成

**Week 4**:
- [ ] 优化对话体验
- [ ] 错误处理和回退
- [ ] 端到端测试
- [ ] 文档编写

### Phase 3：会议模式（2周）

**Week 5**:
- [ ] 实时立场同步机制
- [ ] 增量摘要生成
- [ ] MCP 通知机制
- [ ] 性能测试

**Week 6**:
- [ ] 会议模式 UI 优化
- [ ] 实时性能调优
- [ ] 压力测试
- [ ] 灰度发布

### Phase 4：生产验证（1周）

**Week 7**:
- [ ] 小规模试点（3-5人）
- [ ] 收集反馈
- [ ] 优化体验
- [ ] 全量上线

---

## 七、预期效果 📊

### 7.1 体验提升

| 维度 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| 领导创建事项 | 5分钟（Web操作） | 2分钟（对话） | **↓ 60%** |
| 参与者加入 | 10分钟（注册+加入） | 1分钟（点链接） | **↓ 90%** |
| 学习成本 | 高（需要培训） | 低（自动引导） | **显著降低** |
| 操作步骤 | 8步 | 3步 | **↓ 63%** |

### 7.2 用户满意度

**领导反馈**（预期）:
- 😊 "太方便了，说一句话就能发起会议"
- 😊 "不用再登录网站填表了"
- 😊 "邀请链接一发就行，省事"

**参与者反馈**（预期）:
- 😊 "点个链接就加入了，很顺畅"
- 😊 "引导很清楚，知道该干什么"
- 😊 "不用注册账号，太省心了"

---

## 八、风险与应对 ⚠️

### 8.1 安全风险

**风险 1：邀请链接泄露**

**场景**：链接被转发给无关人员

**应对**：
- 短有效期（默认3天）
- 单次使用（每个链接只能用一次）
- 预期姓名验证（可选）
- 链接撤销功能

```python
@mcp.tool
def revoke_invitation_link(link_id: str) -> dict:
    """撤销邀请链接"""
    # 领导发现链接泄露，立即撤销
```

**风险 2：自动注册被滥用**

**场景**：恶意用户批量消费链接

**应对**：
- 链接使用次数限制
- IP 限流
- 审计日志
- 异常检测告警

### 8.2 体验风险

**风险 1：对话引导失败**

**场景**：用户输入不符合预期，引导卡住

**应对**：
- 提供跳过选项
- 降级到 Web 表单
- 人工客服介入

**风险 2：链接失效**

**场景**：用户3天后才点击链接

**应对**：
- 清晰的过期提示
- 提供重新邀请入口
- 领导可延长有效期

---

## 九、成本分析 💰

### 9.1 开发成本

| 阶段 | 工作量 | 人力 |
|------|--------|------|
| Phase 1（邀请链接） | 2周 | 2人 |
| Phase 2（引导问卷） | 2周 | 2人 |
| Phase 3（会议模式） | 2周 | 2人 |
| Phase 4（生产验证） | 1周 | 2人 |
| **总计** | **7周** | **2人** |

### 9.2 运维成本

**新增资源**：
- 数据库：2个新表（可忽略）
- 存储：每个链接 < 1KB（可忽略）
- 带宽：无明显增加

**节省成本**：
- 减少人工培训成本
- 减少技术支持工单
- 提升会议效率 ROI

---

## 十、总结 ✨

### 核心价值

#### 对领导

✅ **极简操作**
- 一句话发起协作
- 自动生成邀请链接
- 无需学习复杂界面

✅ **快速启动**
- 2分钟创建会议
- 参与者1分钟加入
- 自动配置，零门槛

✅ **灵活模式**
- 会议模式：实时快速决策
- 项目模式：异步深度思考
- 自动识别，智能推荐

#### 对参与者

✅ **无缝加入**
- 点击链接自动加入
- 无需注册账号
- 引导式问卷，清晰明了

✅ **隐私保护**
- 本地 Agent 交互
- 云端只存结论
- 决策模型不出本地

✅ **体验友好**
- 自然语言交互
- 实时立场同步
- AI 辅助收敛

#### 对系统

✅ **架构优雅**
- 基于现有 MCP 协议扩展
- 最小化侵入
- 向后兼容

✅ **可扩展**
- 支持多种协作模式
- 易于添加新功能
- 插件化设计

✅ **安全可靠**
- 链接有效期控制
- 使用次数限制
- 完整审计日志

---

## 附录：与现有系统的关系

### A1. 与批处理方案的结合

**会议模式** = 实时处理（不使用批处理）
**项目模式** = 批处理（使用上一个方案）

两种模式共存，根据场景选择：

```python
if matter.mode == "meeting":
    # 实时处理：立即调用 LLM
    process_immediately(matter_id)
elif matter.mode == "project":
    # 批处理：加入队列
    add_to_batch_queue(matter_id)
```

### A2. 与现有 Web 界面的关系

**兼容共存**：
- Web 界面保留（高级用户使用）
- MCP 工具新增（普通用户使用）
- 数据层统一（同一套 API）

**使用分层**：
- 🎯 **领导** → MCP 一键发起
- 👥 **参与者** → 链接自动加入
- 🔧 **管理员** → Web 精细配置

---

**结论**：该方案将复杂的协作发起流程简化为"对话 + 链接"，大幅降低使用门槛，特别适合没有技术背景的领导层使用。

**投资回报期**：2个月（节省的培训成本 + 提升的会议效率）

**建议**：优先实现 Phase 1 + Phase 2（邀请链接 + 引导问卷），快速验证体验，再决定是否继续 Phase 3（会议模式）。
