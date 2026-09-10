"""Shared FastAPI dependencies for the web layer."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session, sessionmaker

from hub.config import Settings
from hub.db.models import User

SESSION_COOKIE = "hub_session"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_limiter(request: Request):
    return request.app.state.limiter


CSRF_FIELD = "csrf_token"
CSRF_SALT = "hub-csrf"


def get_csrf_serializer(request: Request) -> URLSafeSerializer:
    return URLSafeSerializer(request.app.state.settings.session_secret,
                             salt=CSRF_SALT)


def make_csrf_token(request: Request) -> str:
    """CSRF 同步器 Token：签名绑定当前会话 Cookie（httponly，攻击者不可知）。"""
    return get_csrf_serializer(request).dumps(
        {"sid": request.cookies.get(SESSION_COOKIE, "")})


def register_csrf_globals(templates) -> None:
    """把 csrf_token(request) 注册为 Jinja 全局函数，供表单隐藏域使用。"""
    templates.env.globals[CSRF_FIELD] = make_csrf_token


def _validate_csrf(request: Request) -> None:
    """同步器 Token 校验：签名有效且绑定的会话与当前 Cookie 一致。"""
    # request.form() 已由调用方（async 依赖）读取并缓存
    token = str(request.state._csrf_form.get(CSRF_FIELD, ""))
    try:
        data = get_csrf_serializer(request).loads(token)
    except BadSignature:
        data = {}
    if data.get("sid") != request.cookies.get(SESSION_COOKIE):
        raise HTTPException(status_code=403,
                            detail="CSRF 校验失败，请刷新页面后重试")


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


def get_serializer(request: Request) -> URLSafeSerializer:
    return URLSafeSerializer(request.app.state.settings.session_secret,
                             salt="hub-session")


def set_session_cookie(response, request: Request, user_id: int) -> None:
    value = get_serializer(request).dumps({"uid": user_id})
    response.set_cookie(SESSION_COOKIE, value, httponly=True, samesite="lax")


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = None
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            uid = get_serializer(request).loads(raw).get("uid")
        except BadSignature:
            uid = None
    user = db.get(User, uid) if uid is not None else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if user.must_change_password and request.url.path != "/change-password":
        raise HTTPException(status_code=303, headers={"Location": "/change-password"})
    return user


def get_optional_user(request: Request,
                      db: Session = Depends(get_db)) -> User | None:
    """解析当前会话用户；未登录或会话无效时返回 None，不抛重定向异常。

    用于站点根路径等「按登录态分流」的场景。
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        uid = get_serializer(request).loads(raw).get("uid")
    except BadSignature:
        return None
    user = db.get(User, uid) if uid is not None else None
    if user is None or not user.is_active:
        return None
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def require_csrf(request: Request,
                       user: User = Depends(get_current_user)) -> User:
    """认证态 POST 依赖：先登录校验，再 CSRF 校验。"""
    request.state._csrf_form = await request.form()
    _validate_csrf(request)
    return user


async def require_csrf_admin(request: Request,
                             admin: User = Depends(require_admin)) -> User:
    """管理态 POST 依赖：先管理员校验，再 CSRF 校验。"""
    request.state._csrf_form = await request.form()
    _validate_csrf(request)
    return admin
