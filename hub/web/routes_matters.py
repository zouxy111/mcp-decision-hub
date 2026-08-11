"""Matter pages: dashboard, create form, detail, start action."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.pipeline import BLOCKED_REASON_ROUND_LIMIT
from hub.config import Settings
from hub.db.models import Matter, Output, Round, RoundSummary, Task, User
from hub.web.deps import get_current_user, get_db, get_settings
from hub.web.routes_auth import LLM_NOTICE

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")


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
    user: User = Depends(get_current_user),
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
            task_views = [{"task": t, "assignee": user.username, "output": None}
                          for t in own]
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
            and matter.blocked_reason == BLOCKED_REASON_ROUND_LIMIT
        ),
    }


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
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
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
