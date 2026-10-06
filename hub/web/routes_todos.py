"""待办事项的 JSON 接口（v21，2026-10-07）。

给web 前端用（浏览器 Cookie 会话，**不是** Bearer —— 跟 ``/api/meetings/*``
同一通道）。

**不重复实现业务逻辑**：全部转调 :mod:`hub.mcp_server.methods` 里的
``mcp_*`` 函数。那边是 MCP 与 REST 的单一事实源（既有代码里已是这个惯例，
见 routes_api 里 declare_item / submit_stance 的注释）。两边各写一套的话，
口径迟早会漂 —— 而漂掉的正好是「完成率算多少」「逾期算不算」这种口径，
最难发现也最容易被 agent 当成事实转述出去。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api.errors import ApiError
from hub.config import Settings
from hub.db.models import Matter, MatterParticipant, Todo, User
from hub.domain import todos as todos_svc
from hub.domain.timeutil import utcnow
from hub.mcp_server import methods as m
from hub.web.deps import get_current_user, get_db, get_settings

router = APIRouter(prefix="/api", tags=["todos"])


# --------------------------------------------------------------------------
# 请求体
# --------------------------------------------------------------------------


class CreateTodoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    detail: str | None = Field(default=None, max_length=1000)
    assignee_id: int | None = None
    due_at: str | None = None


class UpdateTodoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    assignee_id: int | None = None
    due_at: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)


class ConfirmTodoIn(BaseModel):
    """确认 AI 抽出来的待办。

    ``assignee_id`` 可选：确认的同时补指派人（AI 认不出负责人时，
    人顺手在确认框里选一个最省事）。
    """

    model_config = ConfigDict(extra="forbid")

    assignee_id: int | None = None


# --------------------------------------------------------------------------
# 事项内的待办
# --------------------------------------------------------------------------


def _check_matter_member(session: Session, matter_id: str, user_id: int) -> Matter:
    """必须是事项发起人或参与人。

    与 :func:`hub.mcp_server.methods._require_matter_visible` 同口径（404 而非
    403），但这里返回 Matter 供界面用，所以自己实现而不是复用。
    """
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    involved = matter.initiator_id == user_id or session.scalar(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter_id,
            MatterParticipant.user_id == user_id,
        )
    ) is not None
    if not involved:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在或不可见")
    return matter


def _participants_of(session: Session, matter_id: str) -> list[dict]:
    """事项参与人名单，供界面渲染「指派给谁」下拉。"""
    rows = session.execute(
        select(MatterParticipant.user_id, MatterParticipant.display_name)
        .where(MatterParticipant.matter_id == matter_id)
    ).all()
    return [{"user_id": uid, "display_name": name or f"user-{uid}"}
            for uid, name in rows]


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


@router.get("/matters/{matter_id}/todos")
def list_matter_todos(
    matter_id: str,
    assignee: Annotated[str, Query()] = "all",
    status: Annotated[str, Query()] = "all",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """某事项的全部待办 + 进度统计 + 参与人名单（给界面渲染下拉）。"""
    _check_matter_member(db, matter_id, user.id)

    rows = db.scalars(
        select(Todo).where(Todo.matter_id == matter_id)
        .order_by(Todo.created_at.desc())
    ).all()

    done, pending, rate = todos_svc.completion_rate(rows)
    awaiting = [t for t in rows if t.needs_confirm]

    # 界面用同一种形状渲染，避免前端各写一套
    names = m._resolve_names(db, [t.assignee_id for t in rows])
    items = [
        m._todo_view(t, matter_title=None, assignee_name=names.get(t.assignee_id))
        for t in rows
    ]

    # 前端筛选在服务端做（数据量小，也省得把全量传下去再筛）
    if status != "all":
        rows = [t for t in rows if t.status == status]
        items = [i for i in items if i["status"] == status]
    if assignee == "me":
        rows = [t for t in rows if t.assignee_id == user.id]
        items = [i for i in items if i["assignee"] == names.get(user.id)]
    elif assignee == "none":
        rows = [t for t in rows if t.assignee_id is None]
        items = [i for i in items if i["assignee"] is None]

    return {
        "todos": items,
        "progress": {
            "total": len([t for t in rows
                          if todos_svc.is_official(t) and t.status != "dropped"]),
            "done": done,
            "pending": pending,
            "completion_rate": rate,
            "overdue_count": sum(1 for t in rows if todos_svc.is_overdue(t)),
            "awaiting_confirm": len(awaiting),
        },
        "participants": _participants_of(db, matter_id),
    }


@router.post("/matters/{matter_id}/todos", status_code=201)
def create_todo(
    matter_id: str,
    payload: CreateTodoIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """人手动建待办。直接是正式的（不需要确认）。"""
    result = m.mcp_create_todo(
        db, settings=settings, user_id=user.id, matter_id=matter_id,
        title=payload.title, detail=payload.detail,
        assignee_id=payload.assignee_id, due_at=payload.due_at,
    )
    db.commit()
    return result


@router.patch("/todos/{todo_id}")
def update_todo(
    todo_id: str,
    payload: UpdateTodoIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """改待办。不传的字段不动。"""
    result = m.mcp_update_todo(
        db, settings=settings, user_id=user.id, todo_id=todo_id,
        status=payload.status, assignee_id=payload.assignee_id,
        due_at=payload.due_at, title=payload.title,
    )
    db.commit()
    return result


@router.post("/todos/{todo_id}/confirm")
def confirm_todo(
    todo_id: str,
    payload: ConfirmTodoIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """确认 AI 抽出来的待办 → 转正式。

    这是owner 2026-10-07 定的闸门：AI 抽的都 ``needs_confirm=True``，
    人点一下才计入进度。没确认的 AI 待办不算数（既不进分子也不进分母）。
    """
    todo = db.get(Todo, todo_id)
    if todo is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "待办不存在")
    _check_matter_member(db, todo.matter_id, user.id)

    if not todo.needs_confirm:
        # 已经是正式的，幂等返回而不是报错 —— 用户可能重复点
        return m._todo_view(todo)

    if payload.assignee_id is not None:
        m._require_assignable(db, matter_id=todo.matter_id, user_id=user.id,
                              target_user_id=payload.assignee_id)
        todo.assignee_id = payload.assignee_id

    todo.needs_confirm = False
    todo.updated_at = utcnow()
    db.commit()
    names = m._resolve_names(db, [todo.assignee_id])
    return m._todo_view(todo, assignee_name=names.get(todo.assignee_id))


@router.delete("/todos/{todo_id}")
def delete_todo(
    todo_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除待办。

    只允许删 AI 抽的待办，且必须先「拒绝」——人工建的是留痕，删了就查不到
    「这件事当初是谁定的」。想表达「不做了」请改状态成 dropped。
    """
    todo = db.get(Todo, todo_id)
    if todo is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "待办不存在")
    _check_matter_member(db, todo.matter_id, user.id)

    if not todo.needs_confirm:
        raise ApiError(400, "TODO_NOT_REJECTABLE",
                       "人工建的待办不能删除；不做了请把状态改成「已放弃」")

    db.delete(todo)
    db.commit()
    return {"ok": True, "deleted_id": todo_id}


@router.get("/todos")
def list_my_todos(
    assignee: Annotated[str, Query()] = "me",
    status: Annotated[str, Query()] = "all",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """跨事项列我的待办（首页「我该做什么」用）。转调 MCP 同名实现。"""
    return m.mcp_list_todos(
        db, settings=settings, user_id=user.id, assignee=assignee,
        status=status,
    )


@router.get("/matters/{matter_id}/project_status")
def project_status(
    matter_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    """某事项的项目全貌。与 MCP 的 ``get_project_status`` 同源。"""
    return m.mcp_get_project_status(db, settings=settings, user_id=user.id,
                                    matter_id=matter_id)