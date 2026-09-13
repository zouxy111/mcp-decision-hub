"""Agent 可用的 REST 降级通道（r5Am9i D1–D6）。

与 /mcp 的 4 个 Agent 工具一一对应，调用**同一批** ``hub.mcp_server.methods``
函数、产出经**同一组** ``hub.schemas.mcp_outputs`` 契约 —— D3 模型单一事实源，
两条通道的返回字段集合逐字段相同（D2）。鉴权复用 Bearer（D5 纯 HTTP，无需
任何 MCP 客户端能力，这正是本通道优先级更高的原因）；成功响应携带
``X-Hub-Channel: rest``（D4 降级显式可观测）。错误经全局 ApiError handler
序列化，与 MCP ToolError 的 {error_code, message} 形状一致（D6：4xx 原样
返回，通道本身不做任何「换一条路」的兜底）。
"""

from fastapi import APIRouter, Body, Depends, Response
from sqlalchemy.orm import Session

from hub.config import Settings
from hub.db.models import User
from hub.mcp_server import methods
from hub.web.deps import get_db, get_settings, require_bearer

router = APIRouter()

CHANNEL_HEADER = "X-Hub-Channel"
CHANNEL = "rest"


def _mark_degraded(response: Response) -> None:
    response.headers[CHANNEL_HEADER] = CHANNEL


@router.get("/api/agent/tasks")
def rest_list_pending_tasks(
    response: Response,
    limit: int | None = None,
    cursor: str | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _mark_degraded(response)
    return methods.mcp_list_pending_tasks(
        db, settings, user_id=user.id, limit=limit, cursor=cursor
    )


@router.get("/api/agent/tasks/{task_id}")
def rest_get_task(
    task_id: str,
    response: Response,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _mark_degraded(response)
    return methods.mcp_get_task(db, settings, user_id=user.id, task_id=task_id)


@router.post("/api/agent/tasks/{task_id}/outputs", status_code=201)
def rest_submit_output(
    task_id: str,
    response: Response,
    payload: dict = Body(...),
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _mark_degraded(response)
    payload = dict(payload)
    payload["task_id"] = task_id
    result = methods.mcp_submit_output(db, settings, user_id=user.id,
                                       payload=payload)
    db.commit()
    return result


@router.get("/api/agent/matters/{matter_id}/status")
def rest_get_matter_status(
    matter_id: str,
    response: Response,
    rounds_before: int | None = None,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _mark_degraded(response)
    return methods.mcp_get_matter_status(
        db, settings, user_id=user.id, matter_id=matter_id,
        rounds_before=rounds_before,
    )
