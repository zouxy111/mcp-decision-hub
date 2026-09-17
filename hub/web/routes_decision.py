"""Decision page: resolution draft review and the three decision actions
(FR-20/FR-21/FR-21b, PRD 7.4/7.6, FR-26 conflict message)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.api.resolutions import (
    accept_provisional,
    continue_probing,
    decide_resolution,
    get_latest_resolution,
)
from hub.db.models import RoundSummary, User
from hub.domain.convergence import CONVERGENCE_PROVISIONALLY_READY
from hub.web.deps import get_current_user, get_db, register_csrf_globals, require_csrf

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


def _page_context(db: Session, matter, user: User) -> dict | None:
    resolution = get_latest_resolution(db, matter_id=matter.id)
    if resolution is None:
        return None
    summary = db.scalar(
        select(RoundSummary).where(
            RoundSummary.round_id == resolution.source_round_id,
            RoundSummary.generation_status == "ok",
        )
    )
    convergence = summary.convergence if summary else None
    is_initiator = matter.initiator_id == user.id
    pending = resolution.status == "pending_review"
    return {
        "matter": matter,
        "resolution": resolution,
        "convergence": convergence,
        "is_initiator": is_initiator,
        "can_decide": (
            is_initiator and pending and matter.status == "awaiting_decision"
        ),
        "provisional_choice": (
            is_initiator and pending and matter.status == "in_progress"
            and convergence == CONVERGENCE_PROVISIONALLY_READY
        ),
        "current_user_is_admin": user.is_admin,
        "error": None,
    }


def _render(request, db, matter, user, *, error=None, status_code=200):
    context = _page_context(db, matter, user)
    if context is None:
        return RedirectResponse(f"/matters/{matter.id}", status_code=303)
    context["error"] = error
    return templates.TemplateResponse(request, "decision.html", context,
                                      status_code=status_code)


@router.get("/matters/{matter_id}/decision", response_class=HTMLResponse)
def decision_page(
    request: Request,
    matter_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    return _render(request, db, matter, user)


@router.post("/matters/{matter_id}/decision", response_class=HTMLResponse)
def decision_submit(
    request: Request,
    matter_id: str,
    action: str = Form(""),
    decision: str = Form(""),
    final_text: str = Form(""),
    rationale: str = Form(""),
    version: int = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    matter = matter_svc.get_matter_for_user(db, matter_id=matter_id, user=user)
    if matter is None:
        raise HTTPException(status_code=404, detail="事项不存在或不可见")
    try:
        if action == "decide":
            decide_resolution(
                db, matter_id=matter_id, actor=user, decision=decision,
                expected_version=version,
                final_text=final_text or None, rationale=rationale or None,
                channel="web",  # 裁定 2（2026-09-17）：不可逆事项仅本通道可完结
            )
        elif action == "accept":
            accept_provisional(db, matter_id=matter_id, actor=user)
        elif action == "continue_probing":
            continue_probing(db, matter_id=matter_id, actor=user)
        else:
            raise ApiError(422, "VALIDATION_FAILED", "未知操作")
    except ApiError as e:
        db.commit()  # persist forbidden/invalid-state audit
        if e.error_code == "RESOLUTION_VERSION_CONFLICT":
            current = (e.details or {}).get("current_version", "?")
            message = (
                f"决议版本已变化（当前版本 {current}），请重新加载后再操作"
            )
        else:
            message = e.message
        return _render(request, db, matter, user, error=message,
                       status_code=e.status_code)
    db.commit()
    request.app.state.resume_queue.put_nowait((matter_id, action))
    return RedirectResponse(f"/matters/{matter_id}", status_code=303)
