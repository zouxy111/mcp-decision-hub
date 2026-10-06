"""Web 前端 JSON 认证接口（供 web-ui 同域前端使用）。

与 :mod:`hub.web.routes_auth` 的 HTML 登录**并存且互不影响**：HTML 路由给
Jinja2 页面用（表单提交 + Cookie 会话 + CSRF），本模块给 React 前端用
（JSON 请求体）。两者共用同一套 :func:`hub.api.accounts.authenticate` 与
同一个``hub_session`` Cookie，所以用户在 HTML 侧登录后前端也自动是登录态。

为什么前端走 Cookie 而不是 Bearer：
  - 前端与后端**同域部署**（见 HANDOFF 决策），Cookie 会自动带上，无需前端
    存储 token（省掉 XSS 窃取 token 的风险面）。
  - 现有 ``/api/*`` 路由统一用 ``require_bearer``（Agent 通道，876 个测试
    依赖其行为），**不能改成 Cookie**。所以前端调``/api/*`` 时仍需带
    Bearer——见 :func:`issue_web_token`。

设计取舍：
  - ``POST /api/auth/login`` 走 Cookie 会话（浏览器主路径）；
  - ``POST /api/auth/token`` 同时种下 Cookie 并签发一枚 Agent token，
    前端存内存/localStorage 后可调既有 ``/api/*``。这样前端不需要后端
    新增任何 ``/api/items`` 的Cookie 变体，876 个测试全部不受影响。
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hub.api import accounts, audit, tokens
from hub.db.models import User
from hub.web.deps import (
    clear_session_cookie,
    get_current_user,
    get_db,
    get_limiter,
    get_optional_user,
    get_settings,
    set_session_cookie,
)
from hub.config import Settings
from hub.domain.rate_limit import (
    rate_limit_key_login_ip,
    rate_limit_key_login_username,
)
from hub import metrics

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    """当前登录者信息。前端据此决定显示名与管理员入口。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    is_admin: bool
    must_change_password: bool


class LoginOut(BaseModel):
    """登录成功响应。

    ``access_token`` 仅 ``POST /api/auth/token`` 返回；Cookie 登录时为 None，
    前端靠 Cookie 维持登录态即可。
    """

    user: UserOut
    must_change_password: bool
    access_token: str | None = None
    token_name: str | None = None


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=1, max_length=256)
    confirm_password: str = Field(min_length=1, max_length=256)


# --------------------------------------------------------------------------
# 限流（与 HTML 登录共用同一套 key，避免两通道互相绕过）
# --------------------------------------------------------------------------


def _login_keys(request: Request, username: str) -> list[tuple[str, str]]:
    ip = request.client.host if request.client else "unknown"
    return [
        (rate_limit_key_login_username(username), "username"),
        (rate_limit_key_login_ip(ip), "ip"),
    ]


def _check_rate(request: Request, settings: Settings, limiter, username: str):
    """返回 retry_after 秒数；未超限返回 None。"""
    if limiter is None:
        return None
    for key, dim in _login_keys(request, username):
        limit = (settings.rate_limit_login_username_per_minute
                 if dim == "username"
                 else settings.rate_limit_login_ip_per_minute)
        ok, retry = limiter.check(key, limit=limit)
        if not ok:
            metrics.rate_limited(f"login_{dim}")
            return retry
    return None


def _record_failure(request: Request, settings: Settings, limiter,
                    username: str) -> None:
    if limiter is None:
        return
    for key, dim in _login_keys(request, username):
        limit = (settings.rate_limit_login_username_per_minute
                 if dim == "username"
                 else settings.rate_limit_login_ip_per_minute)
        limiter.allow(key, limit=limit)


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, username=user.username, email=user.email,
                   is_admin=user.is_admin,
                   must_change_password=user.must_change_password)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


@router.post("/login", response_model=LoginOut)
def api_login(
    request: Request,
    response: Response,
    payload: LoginIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limiter=Depends(get_limiter),
):
    """JSON 登录：校验成功后种下 ``hub_session`` Cookie。

    错误一律 401（不区分「用户不存在」与「密码错」），限流沿用 HTML 登录的
    username + IP 双维度。
    """
    retry = _check_rate(request, settings, limiter, payload.username)
    if retry is not None:
        audit.record_audit(db, audit.LOGIN_RATE_LIMITED,
                           detail={"username": payload.username, "channel": "api"})
        db.commit()
        return Response(
            content='{"detail":"尝试过于频繁，请稍后重试"}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry)},
        )

    user = accounts.authenticate(db, payload.username, payload.password)
    if user is None:
        _record_failure(request, settings, limiter, payload.username)
        audit.record_audit(db, audit.LOGIN_FAILED,
                           detail={"username": payload.username, "channel": "api"})
        db.commit()
        return Response(
            content='{"detail":"用户名或密码错误"}',
            status_code=401,
            media_type="application/json",
        )

    audit.record_audit(db, audit.LOGIN_SUCCESS, actor_user_id=user.id)
    db.commit()
    set_session_cookie(response, request, user.id)
    return LoginOut(user=_user_out(user),
                    must_change_password=user.must_change_password)


@router.post("/token", response_model=LoginOut)
def api_issue_token(
    request: Request,
    response: Response,
    payload: LoginIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    limiter=Depends(get_limiter),
):
    """登录并额外签发一枚 Agent token，供前端调用既有 ``/api/*``。

    与 :func:`api_login` 的差别只在于多返回 ``access_token``。token 明文
    仅此一次返回（库里只存 SHA-256），由调用方自行保存。
    """
    retry = _check_rate(request, settings, limiter, payload.username)
    if retry is not None:
        audit.record_audit(db, audit.LOGIN_RATE_LIMITED,
                           detail={"username": payload.username, "channel": "api"})
        db.commit()
        return Response(
            content='{"detail":"尝试过于频繁，请稍后重试"}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry)},
        )

    user = accounts.authenticate(db, payload.username, payload.password)
    if user is None:
        _record_failure(request, settings, limiter, payload.username)
        audit.record_audit(db, audit.LOGIN_FAILED,
                           detail={"username": payload.username, "channel": "api"})
        db.commit()
        return Response(
            content='{"detail":"用户名或密码错误"}',
            status_code=401,
            media_type="application/json",
        )

    audit.record_audit(db, audit.LOGIN_SUCCESS, actor_user_id=user.id)
    _agent_token, plaintext = tokens.issue_token(db, user=user,
                                                name="web-ui")
    db.commit()
    set_session_cookie(response, request, user.id)
    return LoginOut(user=_user_out(user),
                    must_change_password=user.must_change_password,
                    access_token=plaintext,
                    token_name="web-ui")


@router.get("/me", response_model=UserOut)
def api_me(user: User = Depends(get_current_user)):
    """当前登录者。前端启动时调用以判定登录态（Cookie 通道）。"""
    return _user_out(user)


@router.get("/session")
def api_session(user: User | None = Depends(get_optional_user)):
    """登录态探测：未登录返回 ``{"authenticated": false}`` 而非 401。

    前端用它做首屏判断，避免「未登录」被当成错误弹窗。
    """
    if user is None:
        return {"authenticated": False, "user": None,
                "must_change_password": False}
    return {
        "authenticated": True,
        "user": _user_out(user).model_dump(),
        "must_change_password": user.must_change_password,
    }


@router.post("/logout")
def api_logout(response: Response):
    """清除 Cookie。前端本地状态由调用方清理。"""
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/change-password")
def api_change_password(
    payload: ChangePasswordIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """JSON 改密码。与 HTML 版规则一致：两次一致、至少 8 位。

    成功后 ``must_change_password`` 被清零，前端应跳转到主界面。
    """
    if payload.new_password != payload.confirm_password:
        return Response(content='{"detail":"两次输入的密码不一致"}',
                        status_code=400, media_type="application/json")
    if len(payload.new_password) < 8:
        return Response(content='{"detail":"密码至少 8 个字符"}',
                        status_code=400, media_type="application/json")
    accounts.change_password(db, user=user, new_password=payload.new_password)
    db.commit()
    return {"ok": True}