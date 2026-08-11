"""Auth pages: login, forced password change, invitation consume, logout."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from hub.api import accounts, audit
from hub.config import Settings
from hub.db.models import User
from hub.web.deps import (
    clear_session_cookie,
    get_current_user,
    get_db,
    get_settings,
    set_session_cookie,
)

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")

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
):
    user = accounts.authenticate(db, username, password)
    if user is None:
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
    user: User = Depends(get_current_user),
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
):
    try:
        user = accounts.consume_invitation(
            db, username=username, token=invitation_token, new_password=new_password
        )
    except accounts.InvitationError:
        db.commit()  # persist the login_failed audit written by the service
        return _render_login(request, settings, invite_error="邀请凭证无效或已过期")
    db.commit()
    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, request, user.id)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response
