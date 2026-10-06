# mcp-decision-hub JSON API 契约（前端开发依据）

> 由 `scripts/gen_api_doc.py` 从 FastAPI 运行时规范生成，**不要手改**——
> 改了后端就跑一遍重新生成，否则会和代码脱节。
> 后端现有 **38 个** `/api/*` JSON 接口，本文覆盖前端会用到的部分。

## 认证：两条通道，别搞混

| 通道 | 用法| 覆盖的接口 |
|---|---|---|
| **Cookie 会话** | 浏览器自动带 `hub_session`，前端不用管 | `/api/auth/*`、`/api/meetings/*`、`/api/invitations/*` |
| **Bearer token** | `Authorization: Bearer hdt_xxx` | `/api/items/*`、`/api/questions`、`/api/agent/*` |

**为什么前端要同时拿两样**：`/api/items/*` 这批接口原本是给 AI Agent 用的，
统一走 Bearer，且被大量测试锁住行为；改成 Cookie 会牵动 900+ 测试，因此不动它。
前端的做法是登录时调 `/api/auth/token`——一次拿到 Cookie + token：

```ts
const res = await fetch('/api/auth/token', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username, password }),
})
const { access_token } = await res.json()
localStorage.setItem('hub_token', access_token)
// Cookie 浏览器自己管，前端不用碰
```

之后：
- `/api/items/*`、`/api/questions` → 加 `Authorization: Bearer ${token}`
- `/api/auth/*`、`/api/meetings/*`、`/api/invitations/*` → 什么都不用加

**同域部署**（前后端一个域名），不涉及 CORS。

## 错误形状

`/api/*` 的错误统一长这样（`hub/main.py` 的全局 handler）：

```json
{ "error_code": "MATTER_NOT_FOUND", "message": "事项不存在", "details": {} }
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数或状态不对（如会议已结束） |
| 401 | token 无效/被吊销 |
| 403 | 没有权限 |
| 404 | 资源不存在 |
| 409 | 冲突（如本轮已提交立场） |
| 422 | 校验失败，`details.errors` 是逐字段列表 |
| 429 | 触发限流，带 `Retry-After` 响应头 |

⚠ **未登录不是 401**：`get_current_user` 会 **303 跳登录页**。前端要么用
`fetch(..., {redirect: 'manual'})` 拦一下，要么先调 `GET /api/auth/session`
判断登录态（它未登录时返回 200 + `authenticated: false`）。

## 权限要点

- 会议、邀请的创建/列举/撤销：**只有事项发起人**
- 读会议详情、提交立场：**事项发起人或参与人**
- 事项列表：只返回本人可见的（发起人 + 参与人）


---

## 认证（Cookie 会话）

### `POST /api/auth/change-password`

*Api Change Password*


JSON 改密码。与 HTML 版规则一致：两次一致、至少 8 位。

成功后 ``must_change_password`` 被清零，前端应跳转到主界面。

**请求体**：`ChangePasswordIn`

- `new_password`: string **必填**
- `confirm_password`: string **必填**

---
### `POST /api/auth/login`

*Api Login*


JSON 登录：校验成功后种下 ``hub_session`` Cookie。

错误一律 401（不区分「用户不存在」与「密码错」），限流沿用 HTML 登录的
username + IP 双维度。

**请求体**：`LoginIn`

- `username`: string **必填**
- `password`: string **必填**

**响应**：`LoginOut`

- `user`: UserOut **必填**
- `must_change_password`: boolean **必填**
- `access_token`: string | null
- `token_name`: string | null

---
### `POST /api/auth/logout`

*Api Logout*


清除 Cookie。前端本地状态由调用方清理。

---
### `GET /api/auth/me`

*Api Me*


当前登录者。前端启动时调用以判定登录态（Cookie 通道）。

**响应**：`UserOut`

- `id`: integer **必填**
- `username`: string **必填**
- `email`: string **必填**
- `is_admin`: boolean **必填**
- `must_change_password`: boolean **必填**

---
### `GET /api/auth/session`

*Api Session*


登录态探测：未登录返回 ``{"authenticated": false}`` 而非 401。

前端用它做首屏判断，避免「未登录」被当成错误弹窗。

---
### `POST /api/auth/token`

*Api Issue Token*


登录并额外签发一枚 Agent token，供前端调用既有 ``/api/*``。

与 :func:`api_login` 的差别只在于多返回 ``access_token``。token 明文
仅此一次返回（库里只存 SHA-256），由调用方自行保存。

**请求体**：`LoginIn`

- `username`: string **必填**
- `password`: string **必填**

**响应**：`LoginOut`

- `user`: UserOut **必填**
- `must_change_password`: boolean **必填**
- `access_token`: string | null
- `token_name`: string | null

---

---

## 事项：留言板 / 立场 / 消息 / 决议 / 看板协作

### `GET /api/items?`

*List Items*


列出本人可见的事项（rpQt6D 端点缺口之一）。

与 MCP 工具 ``list_items`` 调用**同一个** ``methods.mcp_list_items``；
筛选值白名单（``participant=me`` / ``state=open``）也在那一侧，本路由
只转交 query 参数，不再判一遍。

**Query 参数**

- `participant`: ?
- `state`: ?

---
### `POST /api/items`

*Declare Item*


声明一个事项 = 一块留言板（rpQt6D 端点缺口之一）。

与 MCP 工具 ``declare_item`` 调用**同一个** ``methods.mcp_declare_item``
—— D3 单一事实源：入参过 ``DeclareItemIn``、出参过 ``DeclareItemOut``，
参与人（可空）与不可逆标记等判定全在那一侧，本路由不重复判。
2026-10-04 起声明出来的就是留言板（status=open），不再走轮次流程。

**请求体**：`object`

---
### `POST /api/items/{matter_id}/ask`

*Ask Participant*


向某位参与人发起定向追问（r5Am9i 第 5 个工具 / rpQt6D 端点「ask」）。

与 MCP 工具 ``ask_participant`` 调用**同一个**
``methods.mcp_ask_participant`` —— D3 单一事实源。**无配额**（owner
2026-09-14 裁决）；本路由只做身份注入，不判配额、不判参与人。

**请求体**：`object`

---
### `GET /api/items/{matter_id}/board_summary`

*Get Board Summary*


这块板的**滚动总结**（云端实时维护，只查库不调模型，毫秒级返回）。

agent 想了解板子现状时先读这里（省上下文），需要原文再 ``list_messages``。
与 MCP 工具 ``get_board_summary`` 同一实现；非成员 404。

**响应**：`BoardSummaryOut`

- `matter_id`: string **必填**
- `summary`: string **必填**
- `judgement`: string **必填**
- `key_points`: array<string> **必填**
- `open_questions`: array<string> **必填**
- `message_count`: integer **必填**
- `covered_messages`: integer **必填**
- `document_version`: integer（默认 `0`）
- `document_count`: integer（默认 `0`）
- `updated_at`: string | null **必填**
- `status`: string **必填**

---
### `GET /api/items/{matter_id}/board_summary/document`

*Get Latest Summary Document*


取**最新一份**总结文档全文（``version`` 省略时的 REST 写法）。

**响应**：`BoardDocumentOut`

- `matter_id`: string **必填**
- `version`: integer **必填**
- `created_at`: string **必填**
- `covered_from`: integer **必填**
- `covered_to`: integer **必填**
- `delta_messages`: integer **必填**
- `trigger`: string **必填**
- `summary`: string **必填**
- `judgement`: string **必填**
- `key_points`: array<string> **必填**
- `open_questions`: array<string> **必填**
- `content_md`: string **必填**

---
### `GET /api/items/{matter_id}/board_summary/documents?`

*List Summary Documents*


这块板攒下的**总结文档清单**（最新在前，不含正文）。

每完成一轮总结，云端就落一份 markdown 文档（``version`` 递增）；
要读全文用下面那个带 ``{version}`` 的端点。

**Query 参数**

- `limit`: ?

**响应**：`BoardDocumentListOut`

- `matter_id`: string **必填**
- `documents`: array<BoardDocumentBriefOut> **必填**
- `document_count`: integer **必填**

---
### `GET /api/items/{matter_id}/board_summary/documents/{version}`

*Get Summary Document*


取一份**总结文档全文**（markdown）。

**响应**：`BoardDocumentOut`

- `matter_id`: string **必填**
- `version`: integer **必填**
- `created_at`: string **必填**
- `covered_from`: integer **必填**
- `covered_to`: integer **必填**
- `delta_messages`: integer **必填**
- `trigger`: string **必填**
- `summary`: string **必填**
- `judgement`: string **必填**
- `key_points`: array<string> **必填**
- `open_questions`: array<string> **必填**
- `content_md`: string **必填**

---
### `POST /api/items/{matter_id}/board_summary/reread`

*Request Board Reread*


有人提了需求：下一轮总结**重读全板**（不是增量）。

常态下云端只读「上次总结之后的新留言」；只有确实需要重读时才调这里。
可以带 ``{"reason": "为什么"}`，理由会写进下一份总结文档。

**响应**：`RereadRequestOut`

- `matter_id`: string **必填**
- `requested`: boolean **必填**
- `message_count`: integer **必填**
- `requested_by_user_id`: integer | null
- `reason`: string | null
- `requested_at`: string **必填**

---
### `POST /api/items/{matter_id}/decide`

*Decide Item*


发起人对决议草案拍板（rpQt6D 端点缺口之一）。

与 MCP 工具 ``decide_item`` 调用**同一个** ``methods.mcp_decide_item``
—— D3 单一事实源。本路由不含任何业务判断，只做身份注入与提交。

裁定 1（2026-09-17）：拍板成功后入 ``resume_queue``，与网页路由
（routes_decision.py）同一完结链路——commit 之后入队，时序与网页
路径逐字同构。此前本通道只写库不入队，事项滞留 awaiting_decision
（柠檬果 2026-09-17 报的 P0）。

**请求体**：`object`

---
### `GET /api/items/{matter_id}/digest`

*Get Item Digest*


一页纸现状：最新 ok 摘要 + 事项状态 + 收敛结果（裁决 3，选 A 简单形态）。

与 MCP 工具 ``get_digest`` 调用**同一个** ``methods.mcp_get_digest``；
成员闸门（非成员 403 ``FORBIDDEN_SCOPE``）也在那一侧，本路由不重复判。

---
### `GET /api/items/{matter_id}/messages?`

*List Item Messages*


读回留言板（时间正序）+ 参与人名片。非成员 404。

**Query 参数**

- `limit`: ?

**响应**：`MessageListOut`

- `matter_id`: string **必填**
- `messages`: array<MessageOut> **必填**
- `participants`: array<ParticipantCardOut> **必填**
- `message_count`: integer **必填**
- `message_limit`: integer **必填**

---
### `POST /api/items/{matter_id}/messages`

*Post Item Message*


在留言板发一条言 / 提问 / 回答。

2026-10-05：``human_approved`` 必填 —— Agent 上传前必须先问过本人并得到同意，
否则 422；``attachment_name`` + ``attachment_md`` 可带 md 文件；
``reply_to_message_id`` 回答云端提问；``ask_user_id`` + ``kind="question"`` 定向提问。

**请求体**：`object`

**响应**：`MessageOut`

- `message_id`: string **必填**
- `matter_id`: string **必填**
- `kind`: string **必填**
- `acting_as`: string **必填**
- `content`: string **必填**
- `created_at`: string **必填**
- `human_approved`: boolean **必填**
- `human_approved_at`: string | null **必填**
- `attachment_name`: string | null **必填**
- `attachment_md`: string | null **必填**
- `asked_to_user_id`: integer | null **必填**
- `reply_to_message_id`: string | null **必填**
- `question_status`: string | null **必填**
- `user_id`: integer **必填**
- `username`: string | null **必填**
- `display_name`: string | null **必填**
- `responsibility`: string | null **必填**

---
### `GET /api/items/{matter_id}/stances`

*List Stances*


本事项下当前用户可见的立场列表（私有字段已裁剪）。

2026-09-17 回填：与 `methods.mcp_list_stances` 同一个实现。

**响应**：`array<StanceListItem>`

---
### `POST /api/items/{matter_id}/stances`

*Submit Stance*


提交一条立场。与 MCP 工具 `submit_stance` 同一个 `methods.mcp_submit_stance`。

2026-09-17 回填：此前本路由直接调 `stance_svc`，是 D3 单一事实源的既存漂移。

**请求体**：`StanceCreate`

- `round_number`: integer **必填**
- `stance`: StanceKind **必填**
- `confidence`: number **必填**
- `position_summary`: string **必填**
- `rationale_summary`: string **必填**
- `non_negotiables`: array<string>
- `conditions`: array<string>
- `open_questions`: array<string>
- `depends_on`: array<string>
- `questions_for`: array<QuestionFor>
- `disagreement_kind`: DisagreementKind | null
- `supersedes`: string | null
- `acting_as`: ActingAs **必填**
- `authority`: string | null
- `ttl_seconds`: integer | null
- `urgency`: Urgency（默认 `normal`）
- `visibility`: Visibility（默认 `participants`）
- `content_hash`: string **必填**

**响应**：`StanceRead`

- `stance_id`: string **必填**
- `matter_id`: string **必填**
- `round_number`: integer **必填**
- `user_id`: integer **必填**
- `stance`: StanceKind **必填**
- `confidence_band`: string **必填**
- `position_summary`: string **必填**
- `rationale_summary`: string **必填**
- `non_negotiables`: array<string> **必填**
- `conditions`: array<string> **必填**
- `open_questions`: array<string> **必填**
- `depends_on`: array<string> **必填**
- `questions_for`: array<QuestionFor> **必填**
- `disagreement_kind`: DisagreementKind | null **必填**
- `supersedes`: string | null **必填**
- `acting_as`: ActingAs **必填**
- `authority`: string | null **必填**
- `ttl_seconds`: integer | null **必填**
- `urgency`: Urgency **必填**
- `visibility`: Visibility **必填**
- `content_hash`: string **必填**
- `created_at`: string **必填**

---
### `GET /api/items/{matter_id}/stances/analysis?`

*Read Stance Analysis*


本轮立场分析（只读）。鉴权与 404 语义复用 stance 既有路由。

2026-09-17 回填：与 MCP 侧同一个 `methods.mcp_read_stance_analysis`。

**Query 参数**

- `round_number`: integer

**响应**：`StanceAnalysisRead`

- `fact_version`: string **必填**
- `sections`: object **必填**
- `violations`: array<string> **必填**
- `limitations`: array<string> **必填**
- `converged`: boolean **必填**
- `degrading`: boolean **必填**
- `undetected_checks`: array<string> **必填**
- `skipped_user_ids`: array<string> **必填**

---
### `GET /api/items/{matter_id}/stances/{user_id}`

*Read Stance*


读取某位参与人最新一轮立场（原值不出门，只出分档，PRD-07）。

2026-09-17 回填：与 MCP 工具 `read_stance` 同一个 `methods.mcp_read_stance`。

**响应**：`StanceRead`

- `stance_id`: string **必填**
- `matter_id`: string **必填**
- `round_number`: integer **必填**
- `user_id`: integer **必填**
- `stance`: StanceKind **必填**
- `confidence_band`: string **必填**
- `position_summary`: string **必填**
- `rationale_summary`: string **必填**
- `non_negotiables`: array<string> **必填**
- `conditions`: array<string> **必填**
- `open_questions`: array<string> **必填**
- `depends_on`: array<string> **必填**
- `questions_for`: array<QuestionFor> **必填**
- `disagreement_kind`: DisagreementKind | null **必填**
- `supersedes`: string | null **必填**
- `acting_as`: ActingAs **必填**
- `authority`: string | null **必填**
- `ttl_seconds`: integer | null **必填**
- `urgency`: Urgency **必填**
- `visibility`: Visibility **必填**
- `content_hash`: string **必填**
- `created_at`: string **必填**

---
### `GET /api/items/{matter_id}/summary`

*Get Item Summary*


事项最新一轮 ok 摘要（rpQt6D 端点缺口之一）。

与 MCP 工具 ``get_summary`` 调用**同一个** ``methods.mcp_get_summary``，
产出过 ``RoundSummaryOut`` 契约。无摘要时那一侧抛 404
``RESOURCE_NOT_FOUND``，本路由不做兜底（两侧错误形状逐字段相同）。

---
### `GET /api/questions?`

*List Pending Questions*


云端提给「你」的、还没回答的问题（本地 Agent 的拉取入口）。

「云端下发 → 本地处理（问过本人、拿到同意）→ 传回回答」这条链路的拉取端：
与 MCP 工具 ``list_pending_questions`` 同一实现。

**Query 参数**

- `matter_id`: ?

**响应**：`PendingQuestionsOut`

- `questions`: array<PendingQuestionOut> **必填**

---

---

## 会议模式

### `GET /api/meetings?`

*List Meetings*


会议列表。

``matter_id`` 可选：给了返回该事项的会议；不给则返回当前用户作为
发起人或参与人的全部会议（前端首页用）。

**Query 参数**

- `matter_id`: ?

**响应**：`array<MeetingBrief>`

---
### `POST /api/meetings`

*Create Meeting*


创建会议

权限：只有事项发起人可以创建会议

**请求体**：`MeetingCreate`

- `matter_id`: string **必填**
- `timeout_minutes`: integer（默认 `3`）

**响应**：`MeetingResponse`

- `meeting_id`: string **必填**
- `matter_id`: string **必填**
- `round_number`: integer **必填**
- `status`: string **必填**
- `timeout_minutes`: integer **必填**

---
### `GET /api/meetings/{meeting_id}`

*Get Meeting*


会议详情：一次返回主题、当前轮立场、参与者与提交状态。

路径顺序注意：本路由必须注册在 ``/{meeting_id}/summary`` 之后不冲突，
且FastAPI 按声明顺序匹配——``/{meeting_id}`` 不会误吞 ``/{meeting_id}/summary``。

**响应**：`MeetingDetail`

- `meeting_id`: string **必填**
- `matter_id`: string **必填**
- `matter_title`: string | null
- `matter_background`: string | null
- `round_number`: integer **必填**
- `status`: string **必填**
- `timeout_minutes`: integer **必填**
- `created_at`: string **必填**
- `started_at`: string | null
- `stances`: array<MeetingStanceOut>（默认 `[]`）
- `participants`: array<object>（默认 `[]`）
- `self_submitted`: boolean（默认 `False`）
- `waiting_for`: array<string>（默认 `[]`）

---
### `POST /api/meetings/{meeting_id}/stances`

*Submit Stance*


提交会议立场

参与人提交本地 LLM 整理后的立场文本。
自动触发收敛检查。

**请求体**：`StanceSubmit`

- `text`: string **必填**
- `round`: integer **必填**

**响应**：`StanceResponse`

- `stance_id`: string **必填**
- `converged`: boolean **必填**
- `summary`: object | null
- `waiting_for`: array | null

---
### `GET /api/meetings/{meeting_id}/summary?`

*Get Summary*


获取会议摘要

用于本地轮询：定期检查收敛是否完成。

**Query 参数**

- `round`: ?

**响应**：`SummaryResponse`

- `ready`: boolean **必填**
- `round`: integer | null
- `summary`: object | null

---

---

## 邀请链接

### `POST /api/invitations/consume/{short_code}`

*Consume Invitation Route*


使用邀请链接注册并加入事项。

无需登录即可访问，用于新用户通过邀请链接注册。

**请求体**：`ConsumeInvitationRequest`

- `username`: string **必填**
- `email`: string **必填**
- `password`: string **必填**
- `display_name`: string **必填** 真实姓名（怎么称呼你）
- `responsibility`: string **必填** 你在本次协作里负责什么

**响应**：`ConsumeInvitationResponse`

- `success`: boolean **必填**
- `user_id`: integer | null
- `username`: string | null
- `matter_id`: string | null
- `error`: string | null

---
### `POST /api/invitations/create`

*Create Invitation*


创建邀请链接。

只有事项的创建者或管理员可以创建邀请链接。

**请求体**：`CreateInvitationRequest`

- `matter_id`: string **必填** 事项 ID
- `expires_in_days`: integer（默认 `3`）  有效期（天数）
- `max_uses`: integer | null（默认 `1`）  最大使用次数（None 表示无限制）
- `invited_name`: string | null  受邀人姓名（可选）

**响应**：`InvitationLinkResponse`

- `id`: string **必填**
- `matter_id`: string **必填**
- `short_code`: string **必填**
- `full_url`: string **必填**
- `created_by`: integer **必填**
- `expires_at`: string **必填**
- `max_uses`: integer | null **必填**
- `used_count`: integer **必填**
- `status`: string **必填**
- `is_active`: boolean **必填**
- `created_at`: string **必填**
- `invited_name`: string | null

---
### `GET /api/invitations/matter/{matter_id}?`

*Get Matter Invitations Route*


获取事项的所有邀请链接。

只有事项的创建者或管理员可以查看。

**Query 参数**

- `include_inactive`: boolean

**响应**：`array<InvitationLinkResponse>`

---
### `GET /api/invitations/validate/{short_code}`

*Validate Invitation Route*


验证邀请链接是否有效（公开）。

返回里带 ``agent_brief``：对方 agent 读这一段就知道怎么注册、怎么接入、
怎么上传文件、以及「上传前必须本人同意」这条硬规矩。

**响应**：`ValidateInvitationResponse`

- `valid`: boolean **必填**
- `matter_id`: string | null
- `matter_title`: string | null
- `invited_name`: string | null
- `expires_at`: string | null
- `agent_brief`: string | null
- `error`: string | null

---
### `POST /api/invitations/{invitation_id}/revoke`

*Revoke Invitation Route*


撤销邀请链接。

只有创建者或事项管理员可以撤销。

---

---

## Agent 通道（Bearer token，前端一般用不到）

### `GET /api/agent/matters/{matter_id}/status?`

*Rest Get Matter Status*

**Query 参数**

- `rounds_before`: ?

---
### `GET /api/agent/tasks?`

*Rest List Pending Tasks*

**Query 参数**

- `limit`: ?
- `cursor`: ?

---
### `GET /api/agent/tasks/{task_id}`

*Rest Get Task*

---
### `POST /api/agent/tasks/{task_id}/outputs`

*Rest Submit Output*


提交产出。收齐本轮后由本通道自己驱动管线。

2026-09-18 回填：此前本路由只写库不入队——MCP 工具 `submit_output`
（tools.py:103-106）那句 ``maybe_drive_round`` + ``drive_queue.put_nowait``
是 2026-08-12（2380632）加的，本通道（f27c8c6）后建时没跟着搬。实测：
参与人全走 REST 提交后轮次停在 open、事项停在 collecting，且重启也救不回。
与裁定 1（REST 拍板不入队）同源同型，故按同一口径修。

2026-09-18 补齐限流：本路由（与 routes_api 共 15 个端点）此前只有认证、
没有配额。提交会驱动轮次进而触发 LLM 调用，无上限刷提交 = 无上限刷账单。
故在 token/account 之外补上第三层 submit 专属限流（PRD 9.1，10/min）。

**请求体**：`object`

---
