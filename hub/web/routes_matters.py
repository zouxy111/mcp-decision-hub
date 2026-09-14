"""Matter pages: dashboard, create form, detail, start action."""

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit as audit_svc
from hub.api import matters as matter_svc
from hub.api import reassignment as reassign_svc
from hub.api.audit_query import (
    DETAIL_AUDIT_PREVIEW,
    MATTER_AUDIT_MAX,
    query_audit_events,
)
from hub.api.errors import ApiError
from hub.api.pipeline import (
    BLOCKED_REASON_DRAFT_FAILED,
    BLOCKED_REASON_ROUND_LIMIT,
)
from hub.api.resolutions import draft_resolution_from_blocked, get_latest_resolution
from hub.config import Settings
from hub.db.models import Matter, MatterParticipant, Output, Round, RoundSummary, Task, User
from hub.web.deps import (
    get_current_user,
    get_db,
    get_settings,
    register_csrf_globals,
    require_csrf,
)
from hub.web.routes_auth import LLM_NOTICE

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    matters = matter_svc.list_matters_for_user(db, user=user)
    if status:
        matters = [m for m in matters if m.status == status]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"matters": matters, "status_filter": status or "",
         "current_user_is_admin": user.is_admin},
    )


@router.get("/matters/new", response_class=HTMLResponse)
def matter_new_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.username)
    ).all()
    return templates.TemplateResponse(
        request, "matter_new.html",
        {"users": users, "error": None, "current_user_id": user.id,
         "current_user_is_admin": user.is_admin,
         "llm_notice": LLM_NOTICE.format(provider=settings.llm_provider_name)},
    )


@router.post("/matters/new", response_class=HTMLResponse)
def matter_create(
    request: Request,
    title: str = Form(""),
    goal: str = Form(""),
    background: str = Form(""),
    questions_text: str = Form(""),
    timeout_hours: int = Form(72),
    participant_ids: list[int] = Form(default=[]),
    initiator_participates: bool = Form(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    ids = list(participant_ids)
    if initiator_participates and user.id not in ids:
        ids.append(user.id)
    try:
        matter = matter_svc.create_matter(
            db,
            initiator=user,
            title=title,
            goal=goal,
            background=background,
            participant_ids=ids,
            initiator_participates=initiator_participates,
            timeout_seconds=timeout_hours * 3600,
            max_rounds=settings.max_rounds,
            draft_questions=questions_text.splitlines(),
        )
    except ApiError as e:
        db.commit()  # persist any audit the service wrote
        users = db.scalars(
            select(User).where(User.is_active.is_(True)).order_by(User.username)
        ).all()
        return templates.TemplateResponse(
            request, "matter_new.html",
            {"users": users, "error": e.message, "current_user_id": user.id,
             "current_user_is_admin": user.is_admin,
             "llm_notice": LLM_NOTICE.format(provider=settings.llm_provider_name)},
        )
    db.commit()
    return RedirectResponse(f"/matters/{matter.id}", status_code=303)


def _build_detail(db: Session, matter: Matter, user: User, settings: Settings) -> dict:
    rounds = db.scalars(
        select(Round).where(Round.matter_id == matter.id)
        .order_by(Round.round_number)
    ).all()
    is_initiator = matter.initiator_id == user.id
    round_views = []
    for rnd in rounds:
        tasks = db.scalars(
            select(Task).where(Task.round_id == rnd.id).order_by(Task.created_at)
        ).all()
        if is_initiator:
            task_views = []
            for t in tasks:
                assignee = db.get(User, t.assignee_id)
                output = db.scalar(select(Output).where(Output.task_id == t.id))
                task_views.append({"task": t, "assignee": assignee.username,
                                   "output": output})
        else:
            own = [t for t in tasks if t.assignee_id == user.id]
            # FR-07：参与人可见本人内容——本人的 Output 原文要随任务带出，
            # 他人 Output 绝不出现在视图里（r9rCtH 修复：此前硬编码 None）。
            task_views = []
            for t in own:
                output = db.scalar(select(Output).where(Output.task_id == t.id))
                task_views.append({"task": t, "assignee": user.username,
                                   "output": output})
        ok_summary = db.scalar(
            select(RoundSummary).where(RoundSummary.round_id == rnd.id,
                                       RoundSummary.generation_status == "ok")
        )
        failed_summary = db.scalar(
            select(RoundSummary).where(RoundSummary.round_id == rnd.id,
                                       RoundSummary.generation_status == "failed")
        )
        round_views.append({
            "round": rnd,
            "tasks_total": len(tasks),
            "tasks_submitted": sum(1 for t in tasks if t.status == "submitted"),
            "task_views": task_views,
            "summary": ok_summary,
            "failed_summary": failed_summary,
        })
    rounds_used = len(rounds)
    auto_limit = matter.max_rounds + matter.granted_extra_rounds
    # 换人（FR-08b）：仅发起人、且事项 collecting/blocked 时可用。换人对象为
    # 当前开放轮中 pending/timeout 的任务。
    can_reassign = (
        is_initiator
        and matter.status in reassign_svc.REASSIGNABLE_MATTER_STATUSES
    )
    reassignable_tasks = []
    if can_reassign and round_views:
        latest = round_views[-1]
        if latest["round"].status == "open":
            for tv in latest["task_views"]:
                if tv["task"].status in reassign_svc.REASSIGNABLE_TASK_STATUSES:
                    reassignable_tasks.append(tv)
    active_users: list[User] = []
    if can_reassign:
        participant_ids = set(db.scalars(
            select(MatterParticipant.user_id).where(
                MatterParticipant.matter_id == matter.id)
        ).all())
        active_users = list(db.scalars(
            select(User).where(User.is_active.is_(True),
                               User.id.not_in(participant_ids) if participant_ids
                               else True)
            .order_by(User.username)
        ).all())
    return {
        "matter": matter,
        "is_initiator": is_initiator,
        "round_views": round_views,
        "llm_provider": settings.llm_provider_name,
        "rounds_used": rounds_used,
        "auto_limit": auto_limit,
        "at_round_limit": rounds_used >= auto_limit,
        "can_continue": (
            is_initiator
            and matter.status == "blocked"
            and (
                matter.blocked_reason == BLOCKED_REASON_ROUND_LIMIT
                or (matter.blocked_reason or "").startswith(
                    BLOCKED_REASON_DRAFT_FAILED
                )
            )
        ),
        "can_cancel": (
            is_initiator
            and matter.status in matter_svc.CANCELLABLE_MATTER_STATUSES
        ),
        "continue_label": (
            "重试生成草案"
            if matter.blocked_reason
            and BLOCKED_REASON_DRAFT_FAILED in matter.blocked_reason
            else "继续（+1 轮）"
        ),
        "draft_pending_generation": (
            matter.status == "in_progress"
            and get_latest_resolution(db, matter_id=matter.id) is None
            and _latest_round_closed_ready(db, matter)
        ),
        "can_draft_from_blocked": (
            is_initiator
            and matter.status == "blocked"
            and (matter.blocked_reason or "").startswith(BLOCKED_REASON_ROUND_LIMIT)
        ),
        "resolution": get_latest_resolution(db, matter_id=matter.id),
        "resolution_convergence": _resolution_convergence(db, matter),
        "can_reassign": can_reassign,
        "reassignable_tasks": reassignable_tasks,
        "active_users": active_users,
        "audit_preview": (
            query_audit_events(db, matter_id=matter.id,
                               limit=DETAIL_AUDIT_PREVIEW)[0]
            if is_initiator else []
        ),
        "audit_usernames": (
            {u.id: u.username for u in db.scalars(select(User)).all()}
            if is_initiator else {}
        ),
    }


def _latest_round_closed_ready(db: Session, matter: Matter) -> bool:
    rnd = db.scalar(
        select(Round).where(Round.matter_id == matter.id)
        .order_by(Round.round_number.desc()).limit(1)
    )
    if rnd is None or rnd.status != "closed":
        return False
    summary = db.scalar(
        select(RoundSummary).where(RoundSummary.round_id == rnd.id,
                                   RoundSummary.generation_status == "ok")
    )
    return summary is not None and summary.convergence in (
        "provisionally_ready", "converged",
    )


def _resolution_convergence(db: Session, matter: Matter) -> str | None:
    resolution = get_latest_resolution(db, matter_id=matter.id)
    if resolution is None:
        return None
    summary = db.scalar(
        select(RoundSummary).where(
            RoundSummary.round_id == resolution.source_round_id,
            RoundSummary.generation_status == "ok",
        )
    )
    return summary.convergence if summary else None


@router.get("/matters/{matter_id}", response_class=HTMLResponse)
def matter_detail(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    context = _build_detail(db, matter, user, settings)
    context["current_user_is_admin"] = user.is_admin
    context["error"] = None
    return templates.TemplateResponse(request, "matter_detail.html", context)


@router.post("/matters/{matter_id}/start", response_class=HTMLResponse)
def matter_start(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    try:
        matter_svc.start_matter(db, matter_id=matter_id, actor=user)
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state audit
        matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
        if matter is None:
            raise HTTPException(status_code=404, detail="事项不存在或不可见") from e
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = e.message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=e.status_code)
    db.commit()
    generating_round_id = db.scalar(
        select(Round.id).where(Round.matter_id == matter_id,
                               Round.status == "generating")
    )
    if generating_round_id is not None:
        request.app.state.drive_queue.put_nowait(generating_round_id)
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)


@router.post("/matters/{matter_id}/continue", response_class=HTMLResponse)
def matter_continue(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    try:
        round_id = matter_svc.continue_matter(db, matter_id=matter_id, actor=user)
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state audit
        matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
        if matter is None:
            raise HTTPException(status_code=404, detail="事项不存在或不可见") from e
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = e.message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=e.status_code)
    db.commit()
    request.app.state.drive_queue.put_nowait(round_id)
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)


@router.post("/matters/{matter_id}/cancel", response_class=HTMLResponse)
def matter_cancel(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """取消事项（FR-08）。取消后任务不可提交，事项进入 cancelled。"""
    try:
        matter_svc.cancel_matter(db, matter_id=matter_id, actor=user)
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state audit
        matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
        if matter is None:
            raise HTTPException(status_code=404, detail="事项不存在或不可见") from e
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = e.message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=e.status_code)
    db.commit()
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)


@router.post("/matters/{matter_id}/reassign", response_class=HTMLResponse)
def matter_reassign(
    request: Request,
    matter_id: str,
    task_id: str = Form(""),
    new_user_id: int = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """换人（FR-08b）：仅发起人，collecting/blocked 下把 pending/timeout 任务
    换给新参与人。换人不触发 LLM；新任务入待办后由各 Agent 自行轮询提交。"""
    try:
        reassign_svc.reassign_task(
            db, matter_id=matter_id, task_id=task_id,
            new_user_id=new_user_id, actor=user,
        )
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state audit
        matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
        if matter is None:
            raise HTTPException(status_code=404, detail="事项不存在或不可见") from e
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = e.message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=e.status_code)
    db.commit()
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)


@router.post("/matters/{matter_id}/draft-resolution", response_class=HTMLResponse)
async def matter_draft_resolution(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """PRD 7.5：轮次上限 blocked 时发起人直接要求生成决议草案。LLM 调用经
    asyncio.to_thread 执行（约束 10）；失败保持 blocked，错误渲染回详情页。"""
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    try:
        resolution = await asyncio.to_thread(
            draft_resolution_from_blocked,
            db, matter_id=matter_id, actor=user, llm=request.app.state.llm,
        )
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state/llm_failed audit
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = e.message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=e.status_code)
    db.commit()
    # 驱动图推进（任务 10 加入闸门后停在闸门等拍板；此前为幂等空转）
    request.app.state.drive_queue.put_nowait(resolution.source_round_id)
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)


@router.get("/matters/{matter_id}/audit", response_class=HTMLResponse)
def matter_audit_page(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    if matter.initiator_id != user.id:
        audit_svc.record_audit(db, audit_svc.FORBIDDEN_DENIED,
                               actor_user_id=user.id, matter_id=matter_id,
                               detail={"action": "view_matter_audit"})
        db.commit()
        raise HTTPException(status_code=403, detail="仅发起人可查看本事项审计")
    rows, _ = query_audit_events(db, matter_id=matter_id,
                                 limit=MATTER_AUDIT_MAX)
    usernames = {
        u.id: u.username
        for u in db.scalars(
            select(User).where(
                User.id.in_([r.actor_user_id for r in rows
                             if r.actor_user_id is not None] or [0])
        )
        ).all()
    }
    return templates.TemplateResponse(
        request, "matter_audit.html",
        {"matter": matter, "rows": rows, "usernames": usernames,
         "current_user_is_admin": user.is_admin},
    )
