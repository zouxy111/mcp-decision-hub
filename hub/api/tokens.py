"""Agent token services (FR-02/FR-03). Plaintext is returned once; only SHA-256 stored."""

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.api.passwords import sha256_hex
from hub.db.models import AgentToken, User
from hub.domain.timeutil import utcnow

# ``last_used_at`` 写回节流窗口（秒）。
#
# 为什么需要：每个带令牌的请求都会走一次 :func:`resolve_user_and_token`。
# 如果每次都写 ``last_used_at`` 再 commit，等于**每个请求都多一次写事务**
# （云端一次 fsync 几十毫秒）。页面上「最近使用时间」精确到分钟就够，
# 所以 60 秒内的重复调用不再写库 —— 「写入提速」里最省事的一项。
LAST_USED_TOUCH_SECONDS = 60


def issue_token(session: Session, *, user: User, name: str) -> tuple[AgentToken, str]:
    plaintext = "hdt_" + secrets.token_urlsafe(32)
    token = AgentToken(user_id=user.id, name=name, token_hash=sha256_hex(plaintext))
    session.add(token)
    session.flush()
    audit.record_audit(session, audit.TOKEN_ISSUED, actor_user_id=user.id,
                       detail={"token_id": token.id, "name": name})
    return token, plaintext


def list_tokens(session: Session, *, user: User) -> list[AgentToken]:
    return list(
        session.scalars(
            select(AgentToken)
            .where(AgentToken.user_id == user.id, AgentToken.revoked_at.is_(None))
            .order_by(AgentToken.created_at.desc())
        ).all()
    )


def revoke_token(session: Session, *, user: User, token_id: str) -> AgentToken:
    token = session.get(AgentToken, token_id)
    if token is None or token.user_id != user.id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Token 不存在")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        session.flush()
        audit.record_audit(session, audit.TOKEN_REVOKED, actor_user_id=user.id,
                           detail={"token_id": token.id, "name": token.name})
    return token


def resolve_user_and_token(
    session: Session, plaintext: str
) -> tuple[User, AgentToken] | None:
    """Resolve a bearer token to (user, token).

    ``last_used_at`` 只在距上次记录超过 :data:`LAST_USED_TOUCH_SECONDS` 秒时写回
    —— 读请求不该因为「留个使用时间」而多写一次库。调用方照常 commit，
    没有脏数据时这次 commit 是空的、不产生 fsync。
    """
    token = session.scalar(
        select(AgentToken).where(
            AgentToken.token_hash == sha256_hex(plaintext),
            AgentToken.revoked_at.is_(None),
        )
    )
    if token is None:
        return None
    user = session.get(User, token.user_id)
    if user is None or not user.is_active:
        return None
    now = utcnow()
    last = token.last_used_at
    if last is None or now - last >= timedelta(seconds=LAST_USED_TOUCH_SECONDS):
        token.last_used_at = now
        session.flush()
    return user, token


def find_user_by_token(session: Session, plaintext: str) -> User | None:
    """Resolve a bearer token to its user（last_used_at 节流规则同上）。"""
    resolved = resolve_user_and_token(session, plaintext)
    if resolved is None:
        return None
    return resolved[0]
