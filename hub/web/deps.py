"""Shared FastAPI dependencies for the web layer."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session, sessionmaker

from hub.api.errors import ApiError
from hub.api.tokens import resolve_user_and_token
from hub.config import Settings
from hub.db.models import User
from hub.domain.rate_limit import (
    rate_limit_key_account,
    rate_limit_key_submit,
    rate_limit_key_token,
)

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


def _enforce_rate_limit(request: Request, *, key: str, limit: int,
                        message: str) -> None:
    """PRD 9.1：滑动窗口超限即 429，并回 Retry-After。

    与 MCP 入口（``mcp_server/auth.py``）共用 ``app.state.limiter`` 实例与
    同一套 key（``token:{id}`` / ``account:{uid}``），所以两个通道的计数
    互相可见——**换通道绕不过配额**。
    """
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is None:            # 未装配限流器的 app（部分单测）直接放行
        return
    ok, retry = limiter.allow(key, limit=limit)
    if not ok:
        raise ApiError(429, "RATE_LIMITED", message,
                       details={"retry_after": retry},
                       headers={"Retry-After": str(retry)})


def enforce_submit_rate_limit(request: Request) -> None:
    """提交产出专属限流（PRD 9.1，10/min per token）。

    与 MCP 侧同型：MCP 在工具层做（``tools.py:83-89``，避开 ASGI body 缓冲），
    这里在路由函数里做。token_id 由 ``require_bearer`` 提前写入 request.state。
    """
    _enforce_rate_limit(request,
                        key=rate_limit_key_submit(request.state.token_id),
                        limit=get_settings(request).rate_limit_submit_per_minute,
                        message="提交产出过于频繁，请稍后重试")


def _resolve_bearer(request: Request, db: Session) -> User:
    """Bearer 认证本体（不含配额校验）。

    复用 resolve_user_and_token（SHA-256 比对 + 吊销/停用校验）作为唯一事实源；
    失败一律 401 AUTH_INVALID_TOKEN，交由全局 ApiError handler 序列化。
    成功后提交以持久化 last_used_at，并把 token_id 挂到 request.state
    供配额（token/submit 两层 key）使用。
    """
    header = request.headers.get("authorization", "")
    plaintext = header[7:].strip() if header.lower().startswith("bearer ") else ""
    resolved = resolve_user_and_token(db, plaintext) if plaintext else None
    if resolved is None:
        raise ApiError(401, "AUTH_INVALID_TOKEN", "Token 缺失、无效或已吊销")
    user, agent_token = resolved
    db.commit()  # 持久化 last_used_at
    request.state.token_id = agent_token.id
    return user


def _enforce_bearer_quota(request: Request, user: User) -> None:
    """PRD 9.1 的 token + account 双层配额。"""
    settings = get_settings(request)
    _enforce_rate_limit(request, key=rate_limit_key_token(request.state.token_id),
                        limit=settings.rate_limit_token_per_minute,
                        message="请求过于频繁，请稍后重试")
    _enforce_rate_limit(request, key=rate_limit_key_account(user.id),
                        limit=settings.rate_limit_account_per_minute,
                        message="请求过于频繁，请稍后重试")


def require_bearer(request: Request, db: Session = Depends(get_db)) -> User:
    """JSON API 依赖：Bearer 认证 + PRD 9.1 配额（token/account 两层）。

    2026-09-18 补齐：此前这两层限流只写在 MCP 入口中间件里，REST 通道
    （本依赖 + routes_api/routes_agent_rest）一处未接。提交端点还会驱动
    轮次进而触发 LLM 调用，无配额 = 无上限刷账单。

    顺序：认证在前、配额在后。无效令牌永远 401，不会因为反复试错变 429
    （否则可用 401/429 的差异探测令牌是否存在）。

    需要「不设配额」的入口用 ``require_bearer_unlimited``，别在此处加路径判断。
    """
    user = _resolve_bearer(request, db)
    _enforce_bearer_quota(request, user)
    return user


def require_bearer_unlimited(request: Request,
                             db: Session = Depends(get_db)) -> User:
    """只认证、不校验配额的 Bearer 依赖（配额体系上的明确豁免口）。

    用于 owner 已明确裁定「不设配额」的入口——这些入口的业务语义本就是
    「可连续追问」，通用配额会把它们掐死：

    - ``POST /api/items/{mid}/stances``：定向提问（questions_for）。
      owner 2026-09-16 更正裁定 1「定向提问**不设配额**」，撤销 09-14 误落地
      的 (matter, actor, target) 滑窗 N=100。守卫：
      ``tests/api/test_rulings_2026_09_16.py::test_定向提问不设配额_超过历史阈值仍可提交``
    - ``POST /api/items/{mid}/ask``：独立追问端点。rpQt6D 验收第 4 条
      「**不存在**针对 ask 的计数/限流」。守卫：
      ``tests/api/test_rpQt6D_ask_endpoint.py::test_无配额_连问一百一十条全部成功``

    ⚠ 已知代价：这两个入口对配额完全豁免，等于配额体系上留了两个口子。
    2026-09-18 补限流时实测发现此冲突（通用配额会拦下它们的连发场景），
    按「既有裁定优先」处理，未自行收口——如需收紧，须 owner 重新裁定。
    """
    return _resolve_bearer(request, db)
