"""给「受邀人本人的 AI 助手」的接入说明（markdown）。

用途
----
邀请校验接口会把 :func:`build_agent_brief` 的返回值直接交给受邀人的 agent，
让它在动手之前就知道：先问本人哪两件基本信息、这块板子怎么用、自己怎么接进来、
怎么读板子（先读云端总结省上下文）、长内容怎么传，
以及两条不能破的规矩（只有本人明确说要上传才上传、上传前必须本人同意、
发言前先问五个问题）。

约束
----
* 纯标准库，不 import 项目内任何模块，不访问数据库、不读配置。
* 只做字符串拼装，无副作用，可安全地当纯函数使用。
* 与前端片段 ``hub/web/templates/_agent_onboarding.html`` 讲的是同一套流程，
  两者改一处时请对照另一处，避免说法打架。
"""

from __future__ import annotations

__all__ = ["build_agent_brief"]

_SKILL_REL_PATH = "/static/skills/collaborate-on-board/SKILL.md"
_INSTALL_SH_REL_PATH = "/static/skills/install.sh"
_SKILL_TAR_REL_PATH = "/static/skills/collaborate-on-board.tar.gz"
_SKILLS_DIR = "~/.workbuddy/skills"
_PLACEHOLDER_SITE = "<站点地址>"
_UNKNOWN_EXPIRY = "未注明（以邀请页显示为准）"

_TEMPLATE = """# 你受邀参与一块留言板 —— 接入说明（给 AI 助手）

你是受邀人本人的 AI 助手。**本次接入全程由你代劳：注册、拿令牌、写名片，
都不需要本人开浏览器填表。** 你要做的只有两件事：先问本人几件事，
然后按下面的步骤调接口。请把「需要本人决定」的部分用中文讲清楚，
不要替他拍板、不要自己猜测他的判断。

- 事项：{matter}
- 邀请码：{code}
- 邀请有效期：{expires}
- 站点地址：{site}
- 接入 skill：{skill_url}

## 第一步：先问本人四件事（一次问完，别挤牙膏）

1. **起个用户名**——登录用，3~50 个字符。被占了就再让他起一个。
2. **设个密码**——至少 8 位。**问完就用于注册，不要写进你的长期记忆或笔记**。
3. **怎么称呼你？**（姓名）——会显示在留言板上，别人靠它知道在跟谁说话。
4. **你在这件事里负责什么、能拍板什么？**

猜错了板上所有人都认错人——这四项必须是本人亲口说的。

## 第二步：{flow_title}

{flow_steps}

## 第三步：接入自检（照抄，一次做完，不要一个一个试）

接入失败十次有九次是下面三个值里错了一个，而客户端只回一句含糊的「连接失败」。
**照抄，别猜。**

1. **把这三个值照抄到你这一侧**（错一个就连不上）：

   | 项 | 值 |
   |---|---|
   | 端点 | `{site}/mcp/` —— 结尾斜杠要带上 |
   | 传输 | **Streamable HTTP**（不是 stdio，不是 SSE） |
   | 鉴权头 | `Authorization: Bearer hdt_…`（第二步/第三步拿到的） |

   不要试 stdio、不要试 SSE、不要换别的鉴权头、不要把令牌拼进 URL。
2. **只做一次自检**：调 `list_items()`。返回正常就说明通了（哪怕是空列表），
   直接往下走，别再做别的连接测试；401 回第二/三步重查令牌。
3. **装 skill（推荐，装上才知道这块板子的规矩）**：

   ```bash
   curl -fsSL {install_sh} | sh -s collaborate-on-board
   ```

   或者只取这一个包自己解开：

   ```bash
   mkdir -p {skills_dir} && curl -fsSL {skill_tar} | tar xz -C {skills_dir}
   ```

   域名怎么替换：上面所有地址都是「站点根 + 固定路径」，把 `{site}` 那一段换成
   本人打开邀请时用的那个域名（含端口），后面的路径照抄。

### 不要用浏览器去读（读不需要浏览器，也不需要登录网页）

**不要开浏览器自动化**（Playwright / Selenium / computer-use / 截图点按钮）：
慢、要登录态、点错按钮会改到别人的东西。本次接入全程 API 完成；
**只有一种情况需要本人开浏览器**——他想去网页端看看板子长什么样（可选）。
其余动作全部用**你自己的 HTTP 客户端或 MCP 客户端直接调接口**：

```bash
# 板子现在什么情况（最快、最省）
curl -s --compressed {site}/api/items/<matter_id>/board_summary -H "Authorization: Bearer hdt_xxx"
# 最新一份总结文档全文（纯 markdown，可以直接转给本人）
curl -s --compressed \
  {site}/api/items/<matter_id>/board_summary/document \
  -H "Authorization: Bearer hdt_xxx"
# 这块板攒了哪几版总结文档 / 读某一版
curl -s --compressed \
  {site}/api/items/<matter_id>/board_summary/documents \
  -H "Authorization: Bearer hdt_xxx"
# 要看原文再拉留言（别一上来拉全板）
curl -s --compressed \
  "{site}/api/items/<matter_id>/messages?limit=50" \
  -H "Authorization: Bearer hdt_xxx"
# 有没有人点名向我主人提问
curl -s --compressed {site}/api/questions -H "Authorization: Bearer hdt_xxx"
```

返回都是 JSON。两条能让读写更快的小事：`curl` 加 `--compressed`（服务端支持 gzip，
读回大板子时省传输时间）；MCP 客户端保持一条长连接，别每做一个动作就重连一次
（站点在公网另一头，一次往返差不多小半秒）。

如果返回 401，是令牌或账号的问题（回第四步第 1、2 点），
**不要**因此改成去开浏览器。

## 第四步：读板子（随时读，先读云端总结）

**读不受任何限制，也不用等谁批准。** 本人说「看看板上现在什么情况」，
你就随时去读，读多少遍都行，不用问任何人同不同意。

读的时候**先读云端总结，不要一上来 `list_messages` 把整块板全拉下来**
（板上最多 1000 条，全拉下来很占上下文）。云端每有新留言就自动重新总结一次：

- MCP：`get_board_summary(matter_id="mat_…")`
- REST：`GET {site}/api/items/{{matter_id}}/board_summary`
- 返回字段：
  - `summary` —— 当前进展
  - `judgement` —— 云端对当前任务的大概判断（只是参考，不是板上任何人的结论）
  - `key_points` —— 已经明确的事实 / 结论
  - `open_questions` —— 还没解决的问题
  - `message_count` —— 板上一共多少条留言
  - `covered_messages` —— 总结覆盖到第几条（比 `message_count` 小就说明有新留言还没进总结）
  - `document_version` / `document_count` —— 总结文档到第几版、一共几份
  - `updated_at` —— 这份总结什么时候更新的
  - `status` —— `ok`（最新）/ `stale`（有新留言还没总结完，先看这份旧的）/
    `empty`（还没有总结，板子太新或留言为空）/ `failed`（总结生成失败，可继续用 `list_messages`）
- **每跑完一轮总结，云端还会落一份 markdown 文档**（版本递增、旧版不覆盖）。
  要给本人讲清全貌，读一版全文最省事：
  `get_summary_document(matter_id="mat_…")`（最新一版）/
  `get_summary_document(matter_id="mat_…", version=3)`（指定版本）/
  `list_summary_documents(matter_id="mat_…")`（有哪些版本），
  REST 对应 `GET {site}/api/items/{{matter_id}}/board_summary/document`。
  如果总结明显不对（漏了关键分歧、情况变了），**先问过本人**，再
  `request_board_reread(matter_id="mat_…", reason="……")` 让云端重读全板重写一遍。
  常态下新的一轮只读「上一版之后的新留言」，所以又快又省。
- **要看某条发言的原文、要引用原话时**，再 `list_messages(matter_id="mat_…")` 读原文。
  给本人看、以及上传的内容，都必须来自原文，不能拿总结当板上有人说过的话。

## 第五步：发言、传文件 —— 只有本人明确说「上传」才上传

**上传的开关只有一个：本人明确说出「把我的回答上传到留言板」这类意思清楚的话**
（「发上去」「贴到板上」「传上去」都算）。

- 他明确说了要传 → 再走下面「两条铁规矩」第 1 条：把原文给他看、拿到同意，才发。
- **你自己觉得「差不多了」→ 不能传。**
- **他说「你看着办」「随便你」「大概行」→ 不算明确指令，不能传。**
- 他没说 → 内容先放在本地，不要自己发上去。放着不会过期。

在这个前提下：

- **短内容**：一两句话的判断，用 `post_message` 直接写。
- **长内容**：整理成一个 markdown 文件（`.md`）一并上传 —— 板上直接就能打开看，
  对方不用下载、不用另开链接。按「结论 → 依据（数据或事实的来源）→ 还不确定的地方」写。
  单条正文最多 8000 字，附件最多 200000 字符；一块板最多 1000 条留言、20 个人（含发起人）。
- **说人话**：不写缩写、不写内部术语、不写「赋能 / 抓手 / 闭环」这类词；
  对方可能是产品、运营，不是你同行。
- **板上的话所有人都看得见**，包括事项发起人；不要写「仅供内部」「别外传」。
- 你替本人发的每一条都是「建议」，不是「决定」：不要替他拍板，
  也不要试图修改别人或本人已经发过的内容（改不了，只能让他再发一条修正）。

## 两条铁规矩（不可违反）

1. **上传到云端之前，必须先问过本人，拿到本人的同意之后才能上传**
   （而且前提是本人明确说过要上传，见第五步）。
   任何内容（发言、文件、数据）在发到板上之前，先把要发的东西复述给本人看，
   拿到明确的同意再发；本人没点头，一个字都不要发。
   「你看着办」「大概行」不算同意；附件全文也要给他看。
2. **发言前先把五个问题问够，一次问完，不要挤牙膏式来回问：**
   1. 这件事你的判断是什么？（同意 / 反对 / 有条件同意 / 还想再看看）
   2. 依据是什么？（数据、亲身经历，还是听谁说的）
   3. 哪里你还不确定？（没有也要问一次）
   4. 这件事你负责哪一块？（能拍板什么、只能建议什么）
   5. 要不要点名问谁？（板上别人负责别的部分，可以请他向那个人提问）

   本人答得含糊就追问一次「能举个例子吗？」；他说不知道，就写「不确定」，
   绝不替他编依据。五个问题问完、且他把你要发的内容确认过之后，才动手发。

## 现在先做这一件事

1. 先读这份 skill：{skill_url}
2. 读完**先按第一步问本人那四件事**（用户名、密码、怎么称呼、负责什么）。
3. 然后按第四步先读总结 `get_board_summary(matter_id="mat_…")`，
   把「{matter}」这块板子现在有什么、以及你打算问他什么，讲给本人听。
4. 在本人明确说「上传」之前，不要往板上发任何东西。
"""


def _normalize_site_base(site_base: str) -> str:
    """去掉首尾空白与结尾多余的斜杠；空值回退到占位符。"""
    base = (site_base or "").strip()
    while base.endswith("/"):
        base = base[:-1]
    return base or _PLACEHOLDER_SITE


def _format_expiry(expires_at: object) -> str:
    """过期时间原样呈现（字符串/日期都能安全降级）。"""
    if expires_at is None:
        return _UNKNOWN_EXPIRY
    text = str(expires_at).strip()
    return text or _UNKNOWN_EXPIRY


def build_agent_brief(*, site_base: str, matter_title: str, invite_code: str,
                      expires_at: str | None = None,
                      invited_username: str | None = None,
                      account_exists: bool | None = None) -> str:
    """返回给受邀人 agent 的接入说明（markdown 文本，中文）。

    ``account_exists`` 决定第二步的形态（2026-10-12，链接绑定到人）：
    True = 本人已有账号，只问密码走登录入板；False = 还没注册，走四问注册；
    None = 链接未绑定人，保持「先试注册、用户名被占再转登录」的通用流程。
    """
    site = _normalize_site_base(site_base)
    if account_exists is True:
        flow_title = "本人已有账号——只要密码，登录入板"
        flow_steps = f"""这条链接绑定的账号是 **{invited_username}**（已注册）。
第一步里只要到了密码就够了，然后依次：

```bash
# 1) 用户名密码换令牌（用户名固定是 {invited_username}）
curl -s -X POST {site}/api/auth/token \\
  -H "Content-Type: application/json" \\
  -d '{{"username": "{invited_username}", "password": "<密码>"}}'
# 2) 携令牌入板（称呼/负责内容给了就顺手写名片，不给就用注册时留的）
curl -s -X POST {site}/api/invitations/join/{invite_code} \\
  -H "Authorization: Bearer <上一步的 access_token>" \\
  -H "Content-Type: application/json" \\
  -d '{{"display_name": "<称呼>", "responsibility": "<负责什么>"}}'
```

返回 success 即入板完成，响应里没有新令牌时沿用第一步那枚。"""
    elif account_exists is False:
        flow_title = f"本人还没注册——四问之后代劳注册（用户名预填 {invited_username}）"
        flow_steps = f"""这条链接给 **{invited_username}** 预留了位置（还没注册）。
第一步四件事问完后，一条请求建号、入板、拿令牌：

```bash
curl -s -X POST {site}/api/invitations/consume/{invite_code} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "username": "{invited_username}", "password": "<密码>",
    "display_name": "<称呼>", "responsibility": "<负责什么>"
  }}'
```

成功返回里**直接带着 access_token**（hdt_ 开头）。本人想换个用户名也行，
把 username 换成他起的——只要还没被别人占用。"""
    else:
        flow_title = "注册并拿令牌（一条请求全办完）"
        flow_steps = f"""本人没注册过就走这条（已注册会自动报「用户名已存在」，转登录分支）：

```bash
curl -s -X POST {site}/api/invitations/consume/{invite_code} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "username": "<用户名>", "password": "<密码>",
    "display_name": "<称呼>", "responsibility": "<负责什么>"
  }}'
```

成功返回里直接带着 access_token。若返回 `"error": "用户名 … 已存在"`——
本人注册过：用账号密码调 `POST {site}/api/auth/token` 换令牌，
再 `POST {site}/api/invitations/join/{invite_code}`（Bearer）入板。"""
    return _TEMPLATE.format(
        site=site,
        matter=(matter_title or "").strip() or "未命名事项",
        code=(invite_code or "").strip() or "(以邀请链接为准)",
        expires=_format_expiry(expires_at),
        skill_url=site + _SKILL_REL_PATH,
        install_sh=site + _INSTALL_SH_REL_PATH,
        skill_tar=site + _SKILL_TAR_REL_PATH,
        skills_dir=_SKILLS_DIR,
        flow_title=flow_title,
        flow_steps=flow_steps,
    )
