"""Matter pages: dashboard, create form, detail, start action."""

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit as audit_svc
from hub.api import board_summary as board_summary_svc
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
from hub.api import task_cards as task_cards_svc
from hub.config import Settings
from hub.db.models import Matter, MatterParticipant, Output, Round, RoundSummary, Task, User
from hub.domain import board as board_svc
from hub.domain.timeutil import utcnow
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
    try:
        # 2026-10-04 形态改造：新建事项一律是**留言板** —— 建好即开放，
        # 不派题、不等 2–5 人凑齐（发起人可以先建板再去发邀请链接）。
        matter = matter_svc.create_board_matter(
            db,
            initiator=user,
            title=title,
            goal=goal,
            background=background,
            participant_ids=ids,
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


def _board_messages_for_web(db: Session, matter: Matter, user: User,
                            limit: int | None = None) -> list[dict]:
    """留言板 Web 视图：时间转成模板约定的 %Y-%m-%dT%H:%M:%SZ（UTC）。

    ``limit`` 默认只取最近 :data:`BOARD_WEB_MESSAGE_LIMIT` 条 —— 留言板容量
    已经放到 1000 条，一页渲染 1000 条（还带 20 万字的附件）会让**浏览器**
    卡住，慢的不是数据库。要一次看全用 ``?messages=all``。
    """
    rows = board_svc.list_messages(db, matter_id=matter.id, user_id=user.id,
                                   limit=limit)
    for row in rows:
        stamp = row.get("created_at") or ""
        row["created_at"] = (stamp.split(".")[0] + "Z") if stamp else "-"
    return rows


# 网页上默认渲染最近多少条留言（接口侧不受这个限制）。
BOARD_WEB_MESSAGE_LIMIT = 100


def _build_detail(db: Session, matter: Matter, user: User, settings: Settings,
                  *, messages_query: str | None = None) -> dict:
    if messages_query and messages_query.strip().lower() == "all":
        web_message_limit = None
    else:
        web_message_limit = BOARD_WEB_MESSAGE_LIMIT
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
        # 留言板形态（2026-10-04）：整个协作界面就是一块留言板，
        # 没有轮次、没有任务、没有提交状态。
        "is_board": matter.mode == "board",
        "board_messages": (
            _board_messages_for_web(db, matter, user, limit=web_message_limit)
            if matter.mode == "board" else []
        ),
        "board_messages_shown": (
            None if web_message_limit is None
            else min(web_message_limit,
                     board_svc.message_count(db, matter_id=matter.id))
        ),
        "board_messages_all": web_message_limit is None,
        "board_participants": (
            board_svc.participant_cards(db, matter_id=matter.id)
            if matter.mode == "board" else []
        ),
        # 「云端提问 → 本地处理 → 传回回答」在网页上的落点：待我回答的提问，
        # 以及板子容量（2026-10-05 甲方要求放到 1000 条）。
        "board_pending_questions": (
            board_svc.list_pending_questions(db, user_id=user.id,
                                             matter_id=matter.id)
            if matter.mode == "board" else []
        ),
        "board_message_count": (
            board_svc.message_count(db, matter_id=matter.id)
            if matter.mode == "board" else 0
        ),
        "board_message_limit": board_svc.MAX_MESSAGES_PER_BOARD,
        # 云端滚动总结：读侧只查库（毫秒级），有新增未总结时 status=stale
        "board_summary": (
            board_summary_svc.summary_view(db, matter_id=matter.id)
            if matter.mode == "board" else None
        ),
        # 每轮总结落一份 md 文档（v20）：网页上给最近 10 份 + 下载入口
        "board_summary_documents": (
            board_summary_svc.list_documents(db, matter_id=matter.id, limit=10)
            if matter.mode == "board" else []
        ),
        "round_views": round_views,
        # 任务卡（v22）：「讨论验收标准 → 发布 → 交付(AI 软审查) → 验收」。
        # 只在留言板上出现；列表给入口，动作全在卡片页。
        "task_cards": (
            task_cards_svc.list_cards(db, matter_id=matter.id, user_id=user.id)
            if matter.mode == "board" else []
        ),
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
    messages: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    context = _build_detail(db, matter, user, settings, messages_query=messages)
    context["current_user_is_admin"] = user.is_admin
    context["error"] = None
    return templates.TemplateResponse(request, "matter_detail.html", context)


# ---------------------------------------------------------------------------
# 云端总结文档（v20，2026-10-05）
#
# 每完成一轮总结就落一份 md 文档。网页上给三件事：看清单、看全文、下载成文件。
# ---------------------------------------------------------------------------


def _summary_document_or_404(db: Session, matter: Matter,
                             version: int | None) -> dict:
    document = board_summary_svc.get_document(
        db, matter_id=matter.id, version=version)
    if document is None:
        raise HTTPException(status_code=404, detail="这块板还没有这一版总结文档")
    return document


@router.get("/matters/{matter_id}/summary/{version}.md")
def matter_summary_document_download(
    matter_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把总结文档下载成本地 .md 文件（可以直接转发给别人）。"""
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    document = _summary_document_or_404(db, matter, version)
    filename = f"summary-v{document['version']}.md"
    return PlainTextResponse(
        document["content_md"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/matters/{matter_id}/summary/{version}", response_class=HTMLResponse)
def matter_summary_document_page(
    request: Request,
    matter_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """看某一版总结文档全文（网页上直接读，不用下载）。"""
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    document = _summary_document_or_404(db, matter, version)
    return templates.TemplateResponse(request, "matter_summary_document.html", {
        "matter": matter,
        "document": document,
        "current_user_is_admin": user.is_admin,
    })


@router.post("/matters/{matter_id}/summary/reread", response_class=HTMLResponse)
def matter_request_reread(
    request: Request,
    matter_id: str,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """有人提了需求：请云端下一轮**重读全板**再重新总结一次。

    常态下云端只读「上次总结之后的新留言」；这条是给「总结已经不对了 / 现在
    的情况跟总结差很远」准备的。提完立刻回页面，后台慢慢跑。
    """
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    try:
        board_summary_svc.request_reread(
            db, matter_id=matter_id, user_id=user.id, reason=reason)
        db.commit()
        request.app.state.board_queue.put_nowait(matter_id)
    except board_svc.BoardError as e:
        db.rollback()
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = str(e)
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=400)
    return RedirectResponse(f"/matters/{matter_id}#cloud-summary", status_code=303)


@router.post("/matters/{matter_id}/messages", response_class=HTMLResponse)
async def matter_post_message(
    request: Request,
    matter_id: str,
    content: str = Form(""),
    kind: str = Form("message"),
    ask_user_id: str = Form(""),
    reply_to_message_id: str = Form(""),
    attachment: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
):
    """在留言板发言 / 提问 / 回答（2026-10-04 起留言板是唯一协作流程）。

    2026-10-05：本条路由是**本人自己在网页上发**，所以同意时间戳直接取当前时间
    （本人操作 = 本人同意）；Agent 走 API/MCP 通道时必须显式带 human_approved。
    支持上传 .md 附件（正文存库，云端可读）。
    """
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")

    def _fail(message: str):
        context = _build_detail(db, matter, user, settings)
        context["current_user_is_admin"] = user.is_admin
        context["error"] = message
        return templates.TemplateResponse(request, "matter_detail.html", context,
                                          status_code=400)

    attachment_name = None
    attachment_md = None
    if attachment is not None and attachment.filename:
        raw = await attachment.read()
        try:
            attachment_md = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _fail("附件要用 UTF-8 编码的纯文本 / markdown 文件")
        attachment_name = attachment.filename

    ask_uid = int(ask_user_id) if ask_user_id.strip().isdigit() else None
    if ask_uid:
        kind = "question"

    try:
        board_svc.post_message(
            db, matter_id=matter_id, user_id=user.id, content=content,
            kind=kind, acting_as="human", human_approved_at=utcnow(),
            attachment_name=attachment_name, attachment_md=attachment_md,
            reply_to_message_id=reply_to_message_id.strip() or None,
            ask_user_id=ask_uid,
        )
        db.commit()
        # 有新留言 → 后台增量更新滚动总结（页面不等它，下次刷新就能看到）
        request.app.state.board_queue.put_nowait(matter_id)
    except board_svc.BoardError as e:
        db.rollback()
        return _fail(str(e))
    return RedirectResponse(f"/matters/{matter_id}#board", status_code=303)


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
