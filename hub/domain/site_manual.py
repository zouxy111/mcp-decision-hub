"""站点对 Agent 的自描述手册（2026-10-12）。

背景：owner 问「把网页链接直接给新电脑上的 Agent，它能不能自己解析？」——
能，前提是链接本身会回答。任意链接带 ``?for_agent=1``（或 Accept 偏好
markdown）时返回本手册而非网页，Agent 拿到任意一条站内链接就能完成冷启动。

纯字符串拼装，不 import 项目内任何模块（与 agent_brief 同一纪律）。
"""

from __future__ import annotations

__all__ = ["build_site_manual", "wants_agent_text"]

_SKILL_REL_PATH = "/static/skills/collaborate-on-board/SKILL.md"

_TEMPLATE = """# MCP 决策中台 · Agent 接入手册

你拿到的链接来自 **MCP 决策中台**——一个多 Agent 协作决策平台
（留言板协作 / 任务卡交付 / 会议收敛 / 待办跟踪）。按下面三步接入，
全程不需要本人开浏览器。

## 冷启动：只需本人记得账号密码

1. **问本人**：用户名、密码（≥8 位）。问完即用于登录，**不要写进长期记忆或笔记**。
2. 换令牌：

   ```bash
   curl -s -X POST {site}/api/auth/token \\
     -H "Content-Type: application/json" \\
     -d '{{"username": "<用户名>", "password": "<密码>"}}'
   ```

   响应里的 access_token（hdt_ 开头）就是长期凭证，本机保存好。
3. 之后所有请求带 `Authorization: Bearer hdt_…`。

## 完整协作手册（必读，免认证可读）

{site}{skill}

里面有：板子怎么读写、发言前必须问本人五个问题、上传前必须本人同意
（human_approved）、任务卡交活规范——**先读它再动手**。

## MCP 通道（推荐，比 REST 顺手）

| 项 | 值 |
|---|---|
| 端点 | `{site}/mcp/`（结尾斜杠带上） |
| 传输 | Streamable HTTP（不是 stdio / SSE） |
| 鉴权 | `Authorization: Bearer hdt_…` |

自检：调 `list_items()`，能看到板子列表即接通。

## 本人还没有账号？

向发起人要一条邀请链接（形如 {site}/invite/xxxx）。
那种链接对 Agent 也是自描述的：直接请求它（加 `?for_agent=1`）
会返回按人定制的接入说明——有账号只问密码，没账号问四件事后代劳注册。
{matter_block}## 一句话总结

问本人要账号密码 → 换令牌 → 读 SKILL → 按它的规矩协作。
"""


def build_site_manual(*, site_base: str,
                      matter_id: str | None = None,
                      matter_title: str | None = None) -> str:
    """给 Agent 的站点手册；带了 matter 信息就附上板子定位指引。"""
    site = (site_base or "").strip().rstrip("/") or "<站点地址>"
    if matter_id:
        matter_block = (
            f"\n## 你手上这条链接对应的板子\n\n"
            f"- 标题：{matter_title or '（未知）'}\n"
            f"- matter_id：`{matter_id}`\n\n"
            f"接入后用 `list_items` 找到它（标题匹配），"
            f"先 `get_board_summary` 读云端总结了解现状。\n"
        )
    else:
        matter_block = ""
    return _TEMPLATE.format(site=site, skill=_SKILL_REL_PATH,
                            matter_block=matter_block)


def wants_agent_text(request) -> bool:
    """判断请求方是不是 Agent（要文本手册而非网页）。"""
    if request.query_params.get("for_agent") == "1":
        return True
    accept = request.headers.get("accept", "")
    return "text/markdown" in accept or "text/plain" in accept
