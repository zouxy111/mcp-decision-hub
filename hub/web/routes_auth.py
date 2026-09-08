"""Auth pages: login, forced password change, invitation consume, logout."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from hub.api import accounts, audit
from hub.config import Settings
from hub.db.models import User
from hub.domain.rate_limit import (
    rate_limit_key_login_ip,
    rate_limit_key_login_username,
)
from hub.web.deps import (
    clear_session_cookie,
    get_current_user,
    get_db,
    get_limiter,
    get_settings,
    register_csrf_globals,
    require_csrf,
    set_session_cookie,
)

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)

LLM_NOTICE = (
    "本事项中提交的回答正文将发送至平台配置的第三方大模型服务商，"
    "用于生成摘要与决议草案。当前服务商：{provider}。"
)


def _render_login(request: Request, settings: Settings, *, error=None,
                  invite_error=None, status_code=200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "invite_error": invite_error,
            "llm_notice": LLM_NOTICE.format(provider=settings.llm_provider_name),
        },
        status_code=status_code,
    )


def _login_keys(request: Request, username: str) -> list[tuple[str, int]]:
    """(limiter key, per-minute limit) pairs for username + client IP."""
    ip = request.client.host if request.client else "unknown"
    return [
        (rate_limit_key_login_username(username), "username"),
        (rate_limit_key_login_ip(ip), "ip"),
    ]


def _login_rate_check(request: Request, settings: Settings, limiter,
                      username: str) -> int | None:
    """Peek failure counters; return retry_after seconds if throttled."""
    if limiter is None:
        return None
    for key, dim in _login_keys(request, username):
        limit = (settings.rate_limit_login_username_per_minute if dim == "username"
                 else settings.rate_limit_login_ip_per_minute)
        ok, retry = limiter.check(key, limit=limit)
        if not ok:
            return retry
    return None


def _login_rate_record(request: Request, settings: Settings, limiter,
                       username: str) -> None:
    """Record one failed attempt on both dimensions."""
    if limiter is None:
        return
    for key, dim in _login_keys(request, username):
        limit = (settings.rate_limit_login_username_per_minute if dim == "username"
                 else settings.rate_limit_login_ip_per_minute)
        limiter.allow(key, limit=limit)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, settings: Settings = Depends(get_settings)):
    return _render_login(request, settings)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limiter=Depends(get_limiter),
):
    retry = _login_rate_check(request, settings, limiter, username)
    if retry is not None:
        audit.record_audit(db, audit.LOGIN_RATE_LIMITED,
                           detail={"username": username})
        db.commit()
        return _render_login(
            request, settings, status_code=429,
            error=f"尝试过于频繁，请 {retry} 秒后重试")
    user = accounts.authenticate(db, username, password)
    if user is None:
        _login_rate_record(request, settings, limiter, username)
        audit.record_audit(db, audit.LOGIN_FAILED, detail={"username": username})
        db.commit()
        return _render_login(request, settings, error="用户名或密码错误")
    audit.record_audit(db, audit.LOGIN_SUCCESS, actor_user_id=user.id)
    db.commit()
    target = "/change-password" if user.must_change_password else "/dashboard"
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, request, user.id)
    return response


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request,
                         user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "change_password.html",
                                      {"error": None})


@router.post("/change-password", response_class=HTMLResponse)
def change_password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "change_password.html", {"error": "两次输入的密码不一致"}
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            request, "change_password.html", {"error": "密码至少 8 个字符"}
        )
    accounts.change_password(db, user=user, new_password=new_password)
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/invite/consume", response_class=HTMLResponse)
def invite_consume(
    request: Request,
    username: str = Form(...),
    invitation_token: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limiter=Depends(get_limiter),
):
    retry = _login_rate_check(request, settings, limiter, username)
    if retry is not None:
        audit.record_audit(db, audit.LOGIN_RATE_LIMITED,
                           detail={"username": username})
        db.commit()
        return _render_login(
            request, settings, status_code=429,
            invite_error=f"尝试过于频繁，请 {retry} 秒后重试")
    try:
        user = accounts.consume_invitation(
            db, username=username, token=invitation_token, new_password=new_password
        )
    except accounts.InvitationError:
        _login_rate_record(request, settings, limiter, username)
        db.commit()  # persist the login_failed audit written by the service
        return _render_login(request, settings, invite_error="邀请凭证无效或已过期")
    db.commit()
    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, request, user.id)
    return response


@router.post("/logout")
def logout(user: User = Depends(require_csrf)):
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response
