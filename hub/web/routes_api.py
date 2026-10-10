"""JSON API 骨架：立场层。

与 hub/web/routes_*.py 的 HTML 路由不同，这里返回 JSON，认证走
Authorization: Bearer（无 Cookie 会话），错误经全局 ApiError handler 序列化。
"""

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from hub.config import Settings
from hub.db.models import User
from hub.mcp_server import methods
from hub.schemas.mcp_outputs import (
    BoardDocumentListOut,
    BoardDocumentOut,
    BoardSummaryOut,
    MessageListOut,
    MessageOut,
    PendingQuestionsOut,
    RereadRequestOut,
)
from hub.schemas.stance import (
    StanceAnalysisRead,
    StanceCreate,
    StanceListItem,
    StanceRead,
)
from hub.web.deps import (
    get_db,
    get_settings,
    require_bearer,
    require_bearer_unlimited,
)

router = APIRouter()

# 与 routes_agent_rest.py 同一标头语义（r5Am9i D4：降级显式可观测）。
# 立场层工具同样有 MCP / REST 两条出口，所以这里也标。
CHANNEL_HEADER = "X-Hub-Channel"
CHANNEL = "rest"


@router.post("/api/items/{matter_id}/stances", status_code=201,
             response_model=StanceRead)
def submit_stance(
    matter_id: str,
    response: Response,
    payload: StanceCreate,
    user: User = Depends(require_bearer_unlimited),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """提交一条立场。与 MCP 工具 `submit_stance` 同一个 `methods.mcp_submit_stance`。

    2026-09-17 回填：此前本路由直接调 `stance_svc`，是 D3 单一事实源的既存漂移。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_submit_stance(
        db, settings, user_id=user.id, matter_id=matter_id,
        payload=payload.model_dump(),
    )
    db.commit()
    return result


# 注意：本路由必须声明在 /stances/{user_id} 之前，否则 "analysis" 会被当成
# user_id 去解析成 int，直接 422。
@router.get("/api/items/{matter_id}/stances/analysis",
            response_model=StanceAnalysisRead)
def read_stance_analysis(
    matter_id: str,
    response: Response,
    round_number: int = 1,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """本轮立场分析（只读）。鉴权与 404 语义复用 stance 既有路由。

    2026-09-17 回填：与 MCP 侧同一个 `methods.mcp_read_stance_analysis`。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    analysis = methods.mcp_read_stance_analysis(
        db, settings, user_id=user.id, matter_id=matter_id,
        round_number=round_number,
    )
    db.commit()  # 落脏数据的收敛降级审计 + 读取留痕
    return analysis


@router.get("/api/items/{matter_id}/stances/{user_id}",
            response_model=StanceRead)
def read_stance(
    matter_id: str,
    user_id: int,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """读取某位参与人最新一轮立场（原值不出门，只出分档，PRD-07）。

    2026-09-17 回填：与 MCP 工具 `read_stance` 同一个 `methods.mcp_read_stance`。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_read_stance(
        db, settings, user_id=user.id, matter_id=matter_id,
        target_user_id=user_id,
    )
    db.commit()  # 落审计（读也留痕）
    return result


@router.get("/api/items/{matter_id}/stances",
            response_model=list[StanceListItem])
def list_stances(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """本事项下当前用户可见的立场列表（私有字段已裁剪）。

    2026-09-17 回填：与 `methods.mcp_list_stances` 同一个实现。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_list_stances(
        db, settings, user_id=user.id, matter_id=matter_id,
    )
    db.commit()
    return result


@router.post("/api/items/{matter_id}/decide")
def decide_item(
    matter_id: str,
    request: Request,
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """发起人对决议草案拍板（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``decide_item`` 调用**同一个** ``methods.mcp_decide_item``
    —— D3 单一事实源。本路由不含任何业务判断，只做身份注入与提交。

    裁定 1（2026-09-17）：拍板成功后入 ``resume_queue``，与网页路由
    （routes_decision.py）同一完结链路——commit 之后入队，时序与网页
    路径逐字同构。此前本通道只写库不入队，事项滞留 awaiting_decision
    （柠檬果 2026-09-17 报的 P0）。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_decide_item(
        db, settings, user_id=user.id, matter_id=matter_id,
        payload=dict(payload),
    )
    db.commit()
    request.app.state.resume_queue.put_nowait((matter_id, "decide"))
    return result


@router.post("/api/items")
def declare_item(
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """声明一个事项 = 一块留言板（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``declare_item`` 调用**同一个** ``methods.mcp_declare_item``
    —— D3 单一事实源：入参过 ``DeclareItemIn``、出参过 ``DeclareItemOut``，
    参与人（可空）与不可逆标记等判定全在那一侧，本路由不重复判。
    2026-10-04 起声明出来的就是留言板（status=open），不再走轮次流程。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_declare_item(
        db, settings, user_id=user.id, payload=dict(payload),
    )
    db.commit()
    return result


@router.get("/api/items")
def list_items(
    response: Response,
    participant: str | None = None,
    state: str | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """列出本人可见的事项（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``list_items`` 调用**同一个** ``methods.mcp_list_items``；
    筛选值白名单（``participant=me`` / ``state=open``）也在那一侧，本路由
    只转交 query 参数，不再判一遍。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_list_items(
        db, settings, user_id=user.id, participant=participant, state=state,
    )


@router.post("/api/items/{matter_id}/ask")
def ask_participant(
    matter_id: str,
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer_unlimited),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """向某位参与人发起定向追问（r5Am9i 第 5 个工具 / rpQt6D 端点「ask」）。

    与 MCP 工具 ``ask_participant`` 调用**同一个**
    ``methods.mcp_ask_participant`` —— D3 单一事实源。**无配额**（owner
    2026-09-14 裁决）；本路由只做身份注入，不判配额、不判参与人。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_ask_participant(
        db, settings, user_id=user.id, matter_id=matter_id,
        payload=dict(payload),
    )
    db.commit()
    return result


@router.get("/api/items/{matter_id}/summary")
def get_item_summary(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """事项最新一轮 ok 摘要（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``get_summary`` 调用**同一个** ``methods.mcp_get_summary``，
    产出过 ``RoundSummaryOut`` 契约。无摘要时那一侧抛 404
    ``RESOURCE_NOT_FOUND``，本路由不做兜底（两侧错误形状逐字段相同）。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_get_summary(
        db, settings, user_id=user.id, matter_id=matter_id,
    )


@router.get("/api/items/{matter_id}/digest")
def get_item_digest(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """一页纸现状：最新 ok 摘要 + 事项状态 + 收敛结果（裁决 3，选 A 简单形态）。

    与 MCP 工具 ``get_digest`` 调用**同一个** ``methods.mcp_get_digest``；
    成员闸门（非成员 403 ``FORBIDDEN_SCOPE``）也在那一侧，本路由不重复判。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_get_digest(
        db, settings, user_id=user.id, matter_id=matter_id,
    )


# ---------------------------------------------------------------------------
# 留言板（2026-10-04 形态改造）：agent 侧的「发言 / 读回」两个端点。
# 与 MCP 工具 post_message / list_messages 同一实现（D3 单一事实源）。
# ---------------------------------------------------------------------------


@router.post("/api/items/{matter_id}/messages", status_code=201,
             response_model=MessageOut)
def post_item_message(
    matter_id: str,
    response: Response,
    request: Request,
    payload: dict = Body(...),
    user: User = Depends(require_bearer_unlimited),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """在留言板发一条言 / 提问 / 回答。

    2026-10-05：``human_approved`` 必填 —— Agent 上传前必须先问过本人并得到同意，
    否则 422；``attachment_name`` + ``attachment_md`` 可带 md 文件；
    ``reply_to_message_id`` 回答云端提问；``ask_user_id`` + ``kind="question"`` 定向提问。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_post_message(
        db, settings, user_id=user.id, matter_id=matter_id,
        payload=dict(payload),
    )
    db.commit()
    # 有新留言 → 后台增量更新这块板的滚动总结（读侧不阻塞）
    request.app.state.board_queue.put_nowait(matter_id)
    return result


@router.get("/api/items/{matter_id}/messages",
            response_model=MessageListOut)
def list_item_messages(
    matter_id: str,
    response: Response,
    limit: int | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """读回留言板（时间正序）+ 参与人名片。非成员 404。"""
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_list_messages(
        db, settings, user_id=user.id, matter_id=matter_id, limit=limit,
    )


@router.get("/api/questions", response_model=PendingQuestionsOut)
def list_pending_questions(
    response: Response,
    matter_id: str | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """云端提给「你」的、还没回答的问题（本地 Agent 的拉取入口）。

    「云端下发 → 本地处理（问过本人、拿到同意）→ 传回回答」这条链路的拉取端：
    与 MCP 工具 ``list_pending_questions`` 同一实现。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_list_pending_questions(
        db, settings, user_id=user.id, matter_id=matter_id,
    )


@router.get("/api/items/{matter_id}/board_summary",
            response_model=BoardSummaryOut)
def get_board_summary(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """这块板的**滚动总结**（云端实时维护，只查库不调模型，毫秒级返回）。

    agent 想了解板子现状时先读这里（省上下文），需要原文再 ``list_messages``。
    与 MCP 工具 ``get_board_summary`` 同一实现；非成员 404。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_get_board_summary(
        db, settings, user_id=user.id, matter_id=matter_id,
    )


@router.get("/api/items/{matter_id}/board_summary/documents",
            response_model=BoardDocumentListOut)
def list_summary_documents(
    matter_id: str,
    response: Response,
    limit: int | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """这块板攒下的**总结文档清单**（最新在前，不含正文）。

    每完成一轮总结，云端就落一份 markdown 文档（``version`` 递增）；
    要读全文用下面那个带 ``{version}`` 的端点。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_list_summary_documents(
        db, settings, user_id=user.id, matter_id=matter_id, limit=limit,
    )


@router.get("/api/items/{matter_id}/board_summary/documents/{version}",
            response_model=BoardDocumentOut)
def get_summary_document(
    matter_id: str,
    version: int,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """取一份**总结文档全文**（markdown）。"""
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_get_summary_document(
        db, settings, user_id=user.id, matter_id=matter_id, version=version,
    )


@router.get("/api/items/{matter_id}/board_summary/document",
            response_model=BoardDocumentOut)
def get_latest_summary_document(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """取**最新一份**总结文档全文（``version`` 省略时的 REST 写法）。"""
    response.headers[CHANNEL_HEADER] = CHANNEL
    return methods.mcp_get_summary_document(
        db, settings, user_id=user.id, matter_id=matter_id, version=None,
    )


@router.post("/api/items/{matter_id}/board_summary/reread",
             response_model=RereadRequestOut)
def request_board_reread(
    matter_id: str,
    response: Response,
    request: Request,
    payload: dict | None = Body(None),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """有人提了需求：下一轮总结**重读全板**（不是增量）。

    常态下云端只读「上次总结之后的新留言」；只有确实需要重读时才调这里。
    可以带 ``{"reason": "为什么"}`，理由会写进下一份总结文档。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    reason = None
    if isinstance(payload, dict):
        raw = payload.get("reason")
        reason = str(raw) if raw else None
    result = methods.mcp_request_board_reread(
        db, settings, user_id=user.id, matter_id=matter_id, reason=reason,
    )
    db.commit()
    request.app.state.board_queue.put_nowait(matter_id)
    return result


# ---------------------------------------------------------------------------
# 任务卡（v22，2026-10-10）：与 MCP 工具 list_task_cards / get_task_card /
# submit_delivery 同一实现（D3 单一事实源）。建卡/发布/验收是发布人的
# 网页动作（Cookie 通道，见 routes_task_cards）。
# ---------------------------------------------------------------------------


@router.get("/api/items/{matter_id}/task_cards")
def list_task_cards(
    matter_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """这块板上的任务卡列表。与 MCP ``list_task_cards`` 同一实现。"""
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_list_task_cards(
        db, settings, user_id=user.id, matter_id=matter_id)
    db.commit()
    return result


@router.get("/api/items/task_cards/{card_id}")
def get_task_card(
    card_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """任务卡全貌：验收标准 + 历次交付 + AI 审查。与 MCP 同一实现。"""
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_get_task_card(
        db, settings, user_id=user.id, card_id=card_id)
    db.commit()
    return result


@router.post("/api/items/task_cards/{card_id}/deliveries", status_code=201)
def submit_delivery(
    card_id: str,
    response: Response,
    request: Request,
    payload: dict = Body(...),
    user: User = Depends(require_bearer_unlimited),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """对 published 卡提交交付，AI 软审查结果随响应返回。

    payload: ``{"summary": "交付说明", "self_check": [{"criterion":..,
    "met": true/false, "note": ".."}]}``。与 MCP ``submit_delivery`` 同一实现。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    body = dict(payload) if isinstance(payload, dict) else {}
    result = methods.mcp_submit_delivery(
        db, settings, user_id=user.id, card_id=card_id,
        summary=str(body.get("summary") or ""),
        self_check=body.get("self_check"),
        llm=request.app.state.llm,
    )
    db.commit()
    return result
