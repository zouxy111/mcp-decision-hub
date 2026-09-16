"""JSON API 骨架：立场层。

与 hub/web/routes_*.py 的 HTML 路由不同，这里返回 JSON，认证走
Authorization: Bearer（无 Cookie 会话），错误经全局 ApiError handler 序列化。
"""

from fastapi import APIRouter, Body, Depends, Response
from sqlalchemy.orm import Session

from hub.api import stances as stance_svc
from hub.config import Settings
from hub.db.models import User
from hub.mcp_server import methods
from hub.schemas.stance import (
    StanceAnalysisRead,
    StanceCreate,
    StanceListItem,
    StanceRead,
)
from hub.web.deps import get_db, get_settings, require_bearer

router = APIRouter()

# 与 routes_agent_rest.py 同一标头语义（r5Am9i D4：降级显式可观测）。
# 立场层工具同样有 MCP / REST 两条出口，所以这里也标。
CHANNEL_HEADER = "X-Hub-Channel"
CHANNEL = "rest"


@router.post("/api/items/{matter_id}/stances", status_code=201,
             response_model=StanceRead)
def submit_stance(
    matter_id: str,
    payload: StanceCreate,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stance = stance_svc.create_stance(
        db, matter_id=matter_id, user=user, payload=payload
    )
    db.commit()
    return stance


# 注意：本路由必须声明在 /stances/{user_id} 之前，否则 "analysis" 会被当成
# user_id 去解析成 int，直接 422。
@router.get("/api/items/{matter_id}/stances/analysis",
            response_model=StanceAnalysisRead)
def read_stance_analysis(
    matter_id: str,
    round_number: int = 1,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    """本轮立场分析（只读）。鉴权与 404 语义复用 stance 既有路由。"""
    analysis = stance_svc.analyze_round(
        db, matter_id=matter_id, round_number=round_number, user=user
    )
    db.commit()  # 落脏数据的收敛降级审计
    return analysis


@router.get("/api/items/{matter_id}/stances/{user_id}",
            response_model=StanceRead)
def read_stance(
    matter_id: str,
    user_id: int,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stance = stance_svc.get_stance(
        db, matter_id=matter_id, user=user, target_user_id=user_id
    )
    db.commit()  # 落审计（读也留痕）
    return stance


@router.get("/api/items/{matter_id}/stances",
            response_model=list[StanceListItem])
def list_stances(
    matter_id: str,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stances = stance_svc.list_stances(db, matter_id=matter_id, user=user)
    db.commit()
    return stances


@router.post("/api/items/{matter_id}/decide")
def decide_item(
    matter_id: str,
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """发起人对决议草案拍板（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``decide_item`` 调用**同一个** ``methods.mcp_decide_item``
    —— D3 单一事实源。本路由不含任何业务判断，只做身份注入与提交。
    """
    response.headers[CHANNEL_HEADER] = CHANNEL
    result = methods.mcp_decide_item(
        db, settings, user_id=user.id, matter_id=matter_id,
        payload=dict(payload),
    )
    db.commit()
    return result


@router.post("/api/items")
def declare_item(
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """声明一个事项（rpQt6D 端点缺口之一）。

    与 MCP 工具 ``declare_item`` 调用**同一个** ``methods.mcp_declare_item``
    —— D3 单一事实源：入参过 ``DeclareItemIn``、出参过 ``DeclareItemOut``，
    参与人数 2–5 与不可逆标记等判定全在那一侧，本路由不重复判。
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
