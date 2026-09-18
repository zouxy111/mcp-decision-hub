---
name: connect-decision-hub
description: 把一个 MCP 决策中台（decision-hub）接进自己的 agent 客户端——签发 Agent Token、填写客户端配置、用一次真实握手自检。当用户问「怎么连 MCP」「签发的 token 怎么接上」「hub 怎么接进 WorkBuddy / Claude」「配 MCP server」「一直 401 AUTH_INVALID_TOKEN」时使用。含端到端实测过的三要素（端点 / 传输 / 鉴权）、最容易漏的前置条件（账号必须先激活）、令牌明文只显示一次的补救方式、以及一张故障对照表。
agent_created: true
---

# 把 decision-hub 接给自己的 agent

## 安装本 skill

装两个（推荐 —— 一个负责连上，一个负责产出决策模型）：

```bash
mkdir -p ~/.workbuddy/skills && curl -fsSL https://hub.tdp-demo.work/static/skills/skills.tar.gz | tar xz -C ~/.workbuddy/skills
```

只装这一个：

```bash
mkdir -p ~/.workbuddy/skills && curl -fsSL https://hub.tdp-demo.work/static/skills/connect-decision-hub.tar.gz | tar xz -C ~/.workbuddy/skills
```

校验 `ls ~/.workbuddy/skills/connect-decision-hub/SKILL.md`；客户端不刷新就重启一次客户端。
不想用命令行，也可以打开 <https://hub.tdp-demo.work/static/skills/index.html> 自行下载。

## 何时用

- 刚部署好 decision-hub，问「我怎么把它接进来」
- 已经签发了 token，但客户端连不上、报 401、或报「传输不兼容」
- 要把 hub 接进 WorkBuddy / Claude Desktop / Claude Code / Cursor 之类 MCP 客户端

**核心纪律**：不要凭「MCP 一般怎么配」去填。**端点路径、传输方式、鉴权头这三个值必须实测确认**。填错任一个，客户端只会回你一句含糊的连接失败，然后你会去怀疑 token ——怀疑错方向。

## 第 0 步：先确认「账号是活的」（最容易漏）

令牌决定**以谁的身份**调用，但**账号本身必须是激活状态**。服务端解析令牌时通常带一道存活检查：

```python
# resolve_user_and_token 内（示意）
if user is None or not user.is_active:
    return None        # → 客户端拿到 401
```

**所以顺序被锁死：先激活账号 → 再签发令牌。**
若账号是邀请制开出来的（初始 `is_active=False`），必须先走完激活流程；否则签出来的令牌**一律 401，而且报错完全看不出是这个原因**。

## 第 1 步：签发令牌

登录 hub → 打开 **`/settings/agents`**（页标题「Agent Token 管理」；注意这是**个人设置页，不是 `/admin/*`**）→「新建 Token」填一个能认出来的名字 →「签发」。

- 明文形如 **`hdt_` + 43 字符 = 47 字符**，**只在签发那一次显示**。
- 库里只存 SHA-256 —— **连管理员也读不回**。没复制到？回这一页把该令牌**吊销**，重新签一枚。**别去数据库里找，找不到。**
- 列表里会显示「从未使用 / 最近使用」。**先确认它写着「从未使用」**，这样一旦连不上，你才能干净地归因到「配置错了」而不是「令牌坏了」。

## 第 2 步：三个关键值（照抄；但在你自己的部署里要会复核）

| 项 | 值 |
|---|---|
| 端点 | `<站点根>/mcp/`（例如 `https://hub.example.com/mcp/`） |
| 传输 | **Streamable HTTP**（不是 stdio，不是 SSE） |
| 鉴权 | 请求头 `Authorization: Bearer hdt_...` |

**怎么自己复核（别照抄我的部署）**：

- **路径**：找 `app.mount("/mcp", mcp_asgi)`，再看 MCP 子应用构造时的 `http_app(path="/")` —— 挂载点 + 内部路径拼出真实端点。带不带尾斜杠通常都能连，但**以实测为准**。
- **传输**：看 MCP 框架的版本与其 `http_app` 的 `transport` 默认值（fastmcp 2.x 默认 `'http'` = Streamable HTTP）。
- **鉴权**：找包在 MCP 端点外面的那层中间件（如 `BearerAuthMiddleware`），看它读哪个请求头、怎么判定无效。

## 第 3 步：写进客户端

WorkBuddy 的落点是 **`~/.workbuddy/mcp.json`**（注意**不是** `.mcp.json`）：

```json
{
  "mcpServers": {
    "decision-hub": {
      "type": "http",
      "url": "https://hub.example.com/mcp/",
      "headers": { "Authorization": "Bearer hdt_把你的那串填这里" }
    }
  }
}
```

**写完不会自动生效。** WorkBuddy 需要到「连接器管理 → 右上角自定义连接器」点一下**信任**，服务才会被加载。改完文件就下结论「没生效」是误判。

其他客户端同理，区别只在字段名。⚠️ 如果某个客户端只支持 `command` + `args`（stdio），那它**不适用**这里的 `type: http` 写法——需要在本地架一个 stdio→HTTP 的桥。

## 第 4 步：自检（别用 curl 冒充验证）

`curl` 能区分「路由通不通」，**但证明不了「拿有效令牌真的能用」**：

```bash
# 只读探测：期望 401 + 应用自己的错误 JSON（而不是反代的 404 / HTML）
curl -sS -o body.json -w '%{http_code}\n' -X POST "$BASE/mcp/" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

- **401 + `{"error_code":"AUTH_INVALID_TOKEN"}`** → 反代转发正常、鉴权层生效 ✅
- **404 或拿到反代自己的 HTML 错误页** → 站点块或路径没配对 ❌（先修这个，别去折腾令牌）

真正的自检要用 **MCP 客户端跑完整握手**：

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    t = StreamableHttpTransport("https://hub.example.com/mcp/",
                                headers={"Authorization": "Bearer hdt_..."})
    async with Client(t, timeout=30) as c:
        tools = await c.list_tools()
        print(len(tools), [x.name for x in tools])

asyncio.run(main())
```

**握手成功 + 能列出工具 = 真的接上了。这一步才算验收。**

## 故障对照表

| 现象 | 多半是 |
|---|---|
| 401，且令牌确定没写错 | **账号未激活**（`is_active=False`）→ 先激活，再重新签发 |
| 401，且账号是活的 | 令牌已被吊销 / 复制时漏字符或带上空格换行 / 头写成 `Bearerhdt_...`（漏了空格） |
| 404 或拿到 HTML | 端点路径错，或反代没把该路径转给应用 |
| 客户端报「传输不兼容」「unexpected content type」 | 客户端按 **SSE** 或 **stdio** 在连，而服务端是 **Streamable HTTP** |
| 列表显示「从未使用」却连不上 | 请求**根本没到服务端** → 查网络与反代，别查令牌 |
| 列表显示「最近使用」却报错 | 请求到了、令牌也对了 → 查账号状态与权限 |

## 安全提醒（交付时要主动讲）

- 令牌 = 以你的身份调用**全部工具**，**等同密码**。不要贴进聊天、不要提交进仓库、不要写进前端。
- 疑似泄露或不再使用 → 立刻在 `/settings/agents` **吊销**；吊销立即生效。
- **一个 agent 一枚令牌**（按用途命名）。多人共用一枚，出事时你无法判断影响范围。

## 实测记录（本 skill 的事实来源）

2026-09-19 在一个真实部署（Caddy 反代 → uvicorn → FastMCP 2.14.7）上端到端验证：

- 无令牌 / 假令牌打 `/mcp/` → **401 + `{"error_code":"AUTH_INVALID_TOKEN","message":"Token 缺失、无效或已吊销"}`**（应用自己的 JSON，非反代 404）
- 真实令牌 + `fastmcp.Client(StreamableHttpTransport(...))` → `initialize` + `tools/list` 成功，列出 **12 个工具**
- 带尾斜杠与不带尾斜杠均可连接
- 吊销后同一令牌立刻失效
