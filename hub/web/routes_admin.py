"""Admin invitation page (PRD 3.1, M1 basic scope: create/resend/revoke),
read-only audit query page (FR-23b), and ops observability page (PRD 10.4)."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import accounts
from hub.api.audit_query import query_audit_events
from hub.api.errors import ApiError
from hub.config import Settings
from hub.db.models import AuditEvent, User
from hub.web.deps import get_db, get_settings, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")


def _pending_invitations(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.invitation_token_hash.is_not(None))
            .order_by(User.created_at.desc())
        ).all()
    )


def _render(request: Request, db: Session, admin: User, *, credential=None,
            credential_for=None, error=None, status_code=200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_invitations.html",
        {
            "pending": _pending_invitations(db),
            "credential": credential,
            "credential_for": credential_for,
            "error": error,
            "current_user_is_admin": True,
        },
        status_code=status_code,
    )


@router.get("/admin/invitations", response_class=HTMLResponse)
def invitations_page(request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    return _render(request, db, admin)


@router.post("/admin/invitations", response_class=HTMLResponse)
def invitations_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    settings: Settings = Depends(get_settings),
):
    if not username.strip() or not email.strip():
        return _render(request, db, admin, error="用户名与邮箱必填", status_code=422)
    user, token = accounts.create_invitation(
        db, admin=admin, username=username.strip(), email=email.strip(),
        ttl_seconds=settings.invite_ttl_seconds,
    )
    db.commit()
    return _render(request, db, admin, credential=token, credential_for=user.username)


@router.post("/admin/invitations/{user_id}/revoke")
def invitations_revoke(user_id: int, db: Session = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        accounts.revoke_invitation(db, admin=admin, user_id=user_id)
    except ApiError:
        pass
    db.commit()
    return RedirectResponse("/admin/invitations", status_code=303)


def _parse_date(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED",
                       "日期格式应为 YYYY-MM-DD") from None
    return parsed + timedelta(days=1) if end_of_day else parsed


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_page(
    request: Request,
    actor: str = "",
    matter_id: str = "",
    event_type: str = "",
    since: str = "",
    until: str = "",
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    error = None
    try:
        since_dt = _parse_date(since or None)
        until_dt = _parse_date(until or None, end_of_day=True)
    except ApiError as e:
        error = e.message
        since_dt = until_dt = None
    actor_user_id = None
    if actor.strip():
        found = db.scalar(select(User.id).where(User.username == actor.strip()))
        actor_user_id = found if found is not None else -1  # 未知账号 → 空结果
    rows, has_more = ([], False)
    if error is None:
        rows, has_more = query_audit_events(
            db, actor_user_id=actor_user_id, matter_id=matter_id.strip() or None,
            event_type=event_type.strip() or None,
            since=since_dt, until=until_dt, offset=max(0, offset),
        )
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
        request,
        "admin_audit.html",
        {
            "rows": rows,
            "usernames": usernames,
            "has_more": has_more,
            "offset": max(0, offset),
        "filters": {"actor": actor, "matter_id": matter_id,
                    "event_type": event_type, "since": since,
                    "until": until},
        "error": error,
        "current_user_is_admin": True,
    },
)


@router.get("/admin/ops", response_class=HTMLResponse)
def admin_ops_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """调度器运行状态与队列深度（PRD 10.4 可观测）。仅管理员。"""
    from hub.api import audit as audit_svc
    from hub.api.scheduler import SCHEDULER_STATE

    state = SCHEDULER_STATE
    # 队列深度：从 app.state 获取（main.py lifespan 设置）
    app = request.app
    drive_qsize = (app.state.drive_queue.qsize()
                   if hasattr(app.state, "drive_queue") else 0)
    resume_qsize = (app.state.resume_queue.qsize()
                    if hasattr(app.state, "resume_queue") else 0)
    # 最近 task_timeout / task_reassigned 审计（各最多 25 条）
    timeout_events = db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit_svc.TASK_TIMEOUT)
        .order_by(AuditEvent.created_at.desc()).limit(25)
    ).all()
    reassign_events = db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit_svc.TASK_REASSIGNED)
        .order_by(AuditEvent.created_at.desc()).limit(25)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_ops.html",
        {
            "scheduler": state,
            "drive_qsize": drive_qsize,
            "resume_qsize": resume_qsize,
            "timeout_events": timeout_events,
            "reassign_events": reassign_events,
            "current_user_is_admin": True,
        },
    )
