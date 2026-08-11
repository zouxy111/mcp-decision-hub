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


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
