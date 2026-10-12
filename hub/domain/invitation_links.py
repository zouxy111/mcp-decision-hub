"""邀请链接业务逻辑：生成、验证、消费。

核心功能：
1. 生成短链码（6位，排除易混淆字符）
2. 验证链接有效性（未过期、未耗尽、未撤销）
3. 消费链接（自动创建账号并加入事项）
"""

from __future__ import annotations

import secrets
import string
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.db.models import (
    InvitationConsumption,
    InvitationLink,
    Matter,
    MatterParticipant,
    User,
)
from hub.domain.timeutil import utcnow

if TYPE_CHECKING:
    pass

# 排除易混淆字符：0/O, I/l/1
SHORT_CODE_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits
SHORT_CODE_ALPHABET = SHORT_CODE_ALPHABET.translate(
    str.maketrans("", "", "0OIl1")
)  # 移除 0, O, I, l, 1

DEFAULT_EXPIRY_DAYS = 3
MAX_GENERATION_ATTEMPTS = 10


class InvitationError(Exception):
    """邀请链接相关错误的基类。"""

    pass


class ShortCodeCollisionError(InvitationError):
    """短链码碰撞（生成多次仍然重复）。"""

    pass


class InvitationExpiredError(InvitationError):
    """链接已过期。"""

    pass


class InvitationExhaustedError(InvitationError):
    """链接已达到最大使用次数。"""

    pass


class InvitationRevokedError(InvitationError):
    """链接已被撤销。"""

    pass


class InvitationNotFoundError(InvitationError):
    """链接不存在。"""

    pass


def generate_short_code(length: int = 6) -> str:
    """生成随机短链码。
    
    Args:
        length: 短链码长度，默认8位
        
    Returns:
        短链码字符串
        
    Examples:
        >>> code = generate_short_code()
        >>> len(code)
        8
        >>> all(c in SHORT_CODE_ALPHABET for c in code)
        True
    """
    return "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(length))


def create_invitation_link(
    db: Session,
    matter_id: str,
    created_by: int,
    invited_name: str | None = None,
    expires_in_days: int | None = None,
    max_uses: int | None = 1,
    invited_username: str | None = None,
    invited_responsibility: str | None = None,
) -> InvitationLink:
    """创建邀请链接。

    Args:
        db: 数据库会话
        matter_id: 事项 ID
        created_by: 创建者用户 ID
        invited_name: 可选的受邀人姓名（展示用）
        expires_in_days: 有效期天数；**None = 永久**（2026-10-12 owner
            裁定：邀请链接不设时限，访问管理靠吊销而非过期）
        max_uses: 最大使用次数，None 表示无限制
        invited_username: 可选的受邀用户名（绑定到人——落地页据此只让
            输密码或预填注册，Agent 据此走对路径）

    Returns:
        创建的邀请链接对象

    Raises:
        ShortCodeCollisionError: 多次尝试后仍然生成重复的短链码
    """
    # 验证 matter 是否存在
    matter = db.get(Matter, matter_id)
    if not matter:
        raise ValueError(f"Matter {matter_id} 不存在")

    # 生成唯一的短链码（带碰撞检测）
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        short_code = generate_short_code()

        # 检查是否已存在
        existing = db.execute(
            select(InvitationLink).where(InvitationLink.short_code == short_code)
        ).scalar_one_or_none()

        if not existing:
            break
    else:
        raise ShortCodeCollisionError(
            f"生成短链码失败：{MAX_GENERATION_ATTEMPTS} 次尝试后仍然碰撞"
        )

    # 创建链接；expires_in_days=None → expires_at=None 永久
    expires_at = (utcnow() + timedelta(days=expires_in_days)
                  if expires_in_days is not None else None)

    invitation = InvitationLink(
        matter_id=matter_id,
        short_code=short_code,
        invited_name=invited_name,
        invited_username=(invited_username or "").strip()[:64] or None,
        invited_responsibility=(invited_responsibility or "").strip()[:256] or None,
        status="active",
        max_uses=max_uses,
        used_count=0,
        expires_at=expires_at,
        created_by=created_by,
        created_at=utcnow(),
    )

    db.add(invitation)
    db.flush()

    return invitation


def get_invitation_by_code(db: Session, short_code: str) -> InvitationLink | None:
    """根据短链码查询邀请链接。
    
    Args:
        db: 数据库会话
        short_code: 短链码
        
    Returns:
        邀请链接对象，不存在则返回 None
    """
    return db.execute(
        select(InvitationLink).where(InvitationLink.short_code == short_code)
    ).scalar_one_or_none()


def validate_invitation(invitation: InvitationLink) -> None:
    """验证邀请链接是否可用。
    
    Args:
        invitation: 邀请链接对象
        
    Raises:
        InvitationExpiredError: 链接已过期
        InvitationExhaustedError: 链接已达到最大使用次数
        InvitationRevokedError: 链接已被撤销
    """
    now = utcnow()
    
    if invitation.status == "revoked":
        raise InvitationRevokedError(f"链接 {invitation.short_code} 已被撤销")

    # expires_at 为 None = 永久链接（2026-10-12），不判过期
    if invitation.expires_at is not None and invitation.expires_at < now:
        raise InvitationExpiredError(
            f"链接 {invitation.short_code} 已过期 "
            f"(expired at {invitation.expires_at.isoformat()})"
        )
    
    # 检查使用次数（None 表示无限制）
    if invitation.max_uses is not None and invitation.used_count >= invitation.max_uses:
        raise InvitationExhaustedError(
            f"链接 {invitation.short_code} 已达到最大使用次数 "
            f"({invitation.used_count}/{invitation.max_uses})"
        )


def consume_invitation_link(
    db: Session,
    short_code: str,
    username: str,
    email: str,
    password_hash: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    display_name: str | None = None,
    responsibility: str | None = None,
) -> tuple[User, InvitationLink]:
    """消费邀请链接：自动创建账号并加入事项。

    完整流程：
    1. 查询并验证链接
    2. 创建用户账号
    3. 加入事项（MatterParticipant）
    4. 记录消费记录
    5. 更新链接使用计数
    6. 如果达到最大使用次数，标记为 consumed

    Args:
        db: 数据库会话
        short_code: 短链码
        username: 用户名
        email: 邮箱
        password_hash: 密码哈希
        ip_address: 请求 IP（可选）
        user_agent: User-Agent（可选）
        display_name: 称呼；**缺省回退链接上发起人填的 invited_name**
        responsibility: 负责事项；**缺省回退 invited_responsibility**
            （2026-10-12 owner 裁定：两项信息前置到发起人创建链接的环节，
            受邀人不再被问；仍允许本人当场改）

    Returns:
        (创建的用户对象, 邀请链接对象)

    Raises:
        InvitationNotFoundError: 链接不存在
        InvitationExpiredError: 链接已过期
        InvitationExhaustedError: 链接已耗尽
        InvitationRevokedError: 链接已撤销
        ValueError: 用户名或邮箱已存在，或称呼/负责事项两头都没有
    """
    # 1. 查询链接
    invitation = get_invitation_by_code(db, short_code)
    if not invitation:
        raise InvitationNotFoundError(f"链接 {short_code} 不存在")

    # 2. 验证链接
    validate_invitation(invitation)

    # 3. 检查用户名和邮箱是否已存在
    existing_username = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if existing_username:
        raise ValueError(f"用户名 {username} 已存在")

    existing_email = db.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    if existing_email:
        raise ValueError(f"邮箱 {email} 已存在")

    # 名片回退：本人没给的，用发起人创建链接时登记的（前置环节，owner 10-12）
    final_display_name = (display_name or "").strip()[:100] \
        or (invitation.invited_name or "").strip()[:100]
    final_responsibility = (responsibility or "").strip() \
        or (invitation.invited_responsibility or "").strip()
    if not final_display_name or not final_responsibility:
        raise ValueError(
            "板上名片不能为空：请填写你的姓名和负责内容"
            "（或让发起人补全链接上的受邀人信息）")

    # 4. 创建用户账号
    user = User(
        username=username,
        email=email,
        password_hash=password_hash,
        is_active=True,
        created_at=utcnow(),
    )
    db.add(user)
    db.flush()  # 确保 user.id 可用

    # 5. 加入事项（名片随注册一起写入：我叫什么 / 我负责什么）
    participant = MatterParticipant(
        matter_id=invitation.matter_id,
        user_id=user.id,
        display_name=final_display_name,
        responsibility=final_responsibility,
    )
    db.add(participant)
    
    # 6. 记录消费记录
    consumption = InvitationConsumption(
        invitation_id=invitation.id,
        user_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        consumed_at=utcnow(),
    )
    db.add(consumption)
    
    # 7. 更新链接状态
    invitation.used_count += 1
    if invitation.max_uses is not None and invitation.used_count >= invitation.max_uses:
        invitation.status = "consumed"
    
    db.flush()
    
    return user, invitation


def join_matter_via_link(
    db: Session,
    short_code: str,
    *,
    user: User,
    display_name: str | None = None,
    responsibility: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> InvitationLink:
    """已有账号经邀请链接加入事项（Agent 代劳接入，2026-10-12）。

    与 :func:`consume_invitation_link` 的差别：不建号、不收密码——
    调用方先用账号密码换 Bearer Token（/api/auth/token），再携 Token
    调本函数。验证、消费记录、计数口径与 consume 完全一致，
    两种入口不会造出不同的计数语义。

    已在板上时幂等：不重复加行，给了名片就顺手更新。
    """
    invitation = get_invitation_by_code(db, short_code)
    if not invitation:
        raise InvitationNotFoundError(f"链接 {short_code} 不存在")

    validate_invitation(invitation)

    existing = db.get(MatterParticipant, (invitation.matter_id, user.id))
    if existing is None:
        db.add(MatterParticipant(
            matter_id=invitation.matter_id,
            user_id=user.id,
            display_name=(display_name or "").strip()[:100] or None,
            responsibility=(responsibility or "").strip() or None,
        ))
    else:
        if display_name and display_name.strip():
            existing.display_name = display_name.strip()[:100]
        if responsibility and responsibility.strip():
            existing.responsibility = responsibility.strip()

    db.add(InvitationConsumption(
        invitation_id=invitation.id,
        user_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        consumed_at=utcnow(),
    ))

    invitation.used_count += 1
    if invitation.max_uses is not None and invitation.used_count >= invitation.max_uses:
        invitation.status = "consumed"

    db.flush()
    return invitation


def revoke_invitation(db: Session, invitation_id: str, revoked_by: int) -> None:
    """撤销邀请链接。

    Args:
        db: 数据库会话
        invitation_id: 邀请链接 ID
        revoked_by: 撤销者用户 ID
    """
    invitation = db.get(InvitationLink, invitation_id)
    if not invitation:
        raise InvitationNotFoundError(f"链接 {invitation_id} 不存在")
    
    invitation.status = "revoked"
    invitation.revoked_at = utcnow()
    invitation.revoked_by = revoked_by
    
    db.flush()


def get_matter_invitations(
    db: Session,
    matter_id: str,
    include_revoked: bool = False,
) -> list[InvitationLink]:
    """获取事项的所有邀请链接。
    
    Args:
        db: 数据库会话
        matter_id: 事项 ID
        include_revoked: 是否包含已撤销的链接
        
    Returns:
        邀请链接列表
    """
    query = select(InvitationLink).where(InvitationLink.matter_id == matter_id)
    
    if not include_revoked:
        query = query.where(InvitationLink.status != "revoked")
    
    return list(db.execute(query).scalars().all())


def cleanup_expired_invitations(db: Session) -> int:
    """清理过期的邀请链接（将状态标记为 expired）。
    
    Args:
        db: 数据库会话
        
    Returns:
        清理的链接数量
    """
    now = utcnow()
    
    expired = db.execute(
        select(InvitationLink).where(
            InvitationLink.status == "active",
            InvitationLink.expires_at.is_not(None),
            InvitationLink.expires_at < now,
        )
    ).scalars().all()
    
    for invitation in expired:
        invitation.status = "expired"
    
    db.flush()
    
    return len(expired)


def validate_invitation_link(db: Session, short_code: str) -> dict:
    """验证邀请链接并返回结果字典（用于 API）。
    
    Args:
        db: 数据库会话
        short_code: 短链码
        
    Returns:
        包含验证结果的字典
    """
    try:
        invitation = get_invitation_by_code(db, short_code)
        if not invitation:
            return {"valid": False, "error": "Invitation not found"}
        
        validate_invitation(invitation)

        matter = db.get(Matter, invitation.matter_id)
        creator = db.get(User, invitation.created_by)

        # 绑定到人（2026-10-12）：平台侧直接给出账号是否存在——拿着这条
        # 链接的人就是受邀人本人，不存在枚举风险；Agent 据此走对路径
        # （有账号只问密码，没账号走注册四问）。
        account_exists = None
        if invitation.invited_username:
            account_exists = db.scalar(
                select(User.id).where(User.username == invitation.invited_username)
            ) is not None

        return {
            "valid": True,
            "matter_id": invitation.matter_id,
            "matter_title": matter.title if matter else "Unknown",
            "invited_name": invitation.invited_name,
            "invited_username": invitation.invited_username,
            "invited_responsibility": invitation.invited_responsibility,
            "account_exists": account_exists,
            "expires_at": (invitation.expires_at.isoformat()
                           if invitation.expires_at else None),
            "creator_name": creator.username if creator else "系统管理员",
            "collaboration_mode": matter.mode if matter else "project",
        }
    except InvitationExpiredError as e:
        return {"valid": False, "error": f"Invitation expired: {e}"}
    except InvitationExhaustedError as e:
        return {"valid": False, "error": f"Invitation exhausted: {e}"}
    except InvitationRevokedError as e:
        return {"valid": False, "error": f"Invitation revoked: {e}"}
    except Exception as e:
        return {"valid": False, "error": str(e)}


def revoke_invitation_link(db: Session, invitation_id: str) -> dict:
    """撤销邀请链接（用于 API）。
    
    Args:
        db: 数据库会话
        invitation_id: 邀请链接 ID
        
    Returns:
        包含结果的字典
    """
    try:
        invitation = db.get(InvitationLink, invitation_id)
        if not invitation:
            return {"success": False, "error": "Invitation not found"}
        
        invitation.is_active = False
        invitation.status = "revoked"
        db.flush()
        
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
