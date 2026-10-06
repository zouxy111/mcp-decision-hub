"""测试邀请链接业务逻辑。"""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from hub.db.models import Matter, User
from hub.domain import invitation_links
from hub.domain.timeutil import utcnow


@pytest.fixture
def test_user(db_session: Session) -> User:
    """创建测试用户（发起人）。"""
    user = User(
        username="test_initiator",
        email="initiator@test.com",
        password_hash="hash123",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def test_matter(db_session: Session, test_user: User) -> Matter:
    """创建测试事项。"""
    matter = Matter(
        initiator_id=test_user.id,
        title="测试决策事项",
        goal="测试邀请链接功能",
        status="draft",
    )
    db_session.add(matter)
    db_session.flush()
    return matter


def test_generate_short_code():
    """测试短链码生成。"""
    code = invitation_links.generate_short_code()
    
    # 检查长度
    assert len(code) == 6
    
    # 检查字符集（不包含易混淆字符）
    assert all(c in invitation_links.SHORT_CODE_ALPHABET for c in code)
    assert "0" not in code
    assert "O" not in code
    assert "I" not in code
    assert "l" not in code
    assert "1" not in code
    
    # 生成多个验证随机性
    codes = {invitation_links.generate_short_code() for _ in range(100)}
    assert len(codes) > 90  # 大概率不重复


def test_create_invitation_link(db_session: Session, test_matter: Matter, test_user: User):
    """测试创建邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
        invited_name="Alice",
    )
    
    assert invitation.id is not None
    assert invitation.matter_id == test_matter.id
    assert invitation.created_by == test_user.id
    assert invitation.invited_name == "Alice"
    assert invitation.status == "active"
    assert invitation.max_uses == 1
    assert invitation.used_count == 0
    assert len(invitation.short_code) == 6
    
    # 检查有效期（默认3天）
    expected_expires = utcnow() + timedelta(days=3)
    assert abs((invitation.expires_at - expected_expires).total_seconds()) < 5


def test_create_invitation_link_custom_expiry(
    db_session: Session, test_matter: Matter, test_user: User
):
    """测试自定义有效期。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
        expires_in_days=7,
    )
    
    expected_expires = utcnow() + timedelta(days=7)
    assert abs((invitation.expires_at - expected_expires).total_seconds()) < 5


def test_get_invitation_by_code(db_session: Session, test_matter: Matter, test_user: User):
    """测试根据短链码查询。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.flush()
    
    # 正常查询
    found = invitation_links.get_invitation_by_code(db_session, invitation.short_code)
    assert found is not None
    assert found.id == invitation.id
    
    # 不存在的短链码
    not_found = invitation_links.get_invitation_by_code(db_session, "NOTEXIST")
    assert not_found is None


def test_validate_invitation_success(db_session: Session, test_matter: Matter, test_user: User):
    """测试验证有效的邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    
    # 应该不抛出异常
    invitation_links.validate_invitation(invitation)


def test_validate_invitation_expired(db_session: Session, test_matter: Matter, test_user: User):
    """测试验证过期的邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    
    # 手动设置为过期
    invitation.expires_at = utcnow() - timedelta(days=1)
    
    with pytest.raises(invitation_links.InvitationExpiredError):
        invitation_links.validate_invitation(invitation)


def test_validate_invitation_exhausted(db_session: Session, test_matter: Matter, test_user: User):
    """测试验证已耗尽的邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
        max_uses=1,
    )
    
    # 手动设置为已用完
    invitation.used_count = 1
    
    with pytest.raises(invitation_links.InvitationExhaustedError):
        invitation_links.validate_invitation(invitation)


def test_validate_invitation_revoked(db_session: Session, test_matter: Matter, test_user: User):
    """测试验证已撤销的邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    
    # 撤销链接
    invitation.status = "revoked"
    
    with pytest.raises(invitation_links.InvitationRevokedError):
        invitation_links.validate_invitation(invitation)


def test_consume_invitation_link_success(
    db_session: Session, test_matter: Matter, test_user: User
):
    """测试成功消费邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    # 消费链接
    user, consumed_invitation = invitation_links.consume_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
        username="alice",
        email="alice@test.com",
        password_hash="hash456",
        ip_address="127.0.0.1",
        user_agent="TestAgent/1.0",
    )
    db_session.commit()
    
    # 验证用户已创建
    assert user.id is not None
    assert user.username == "alice"
    assert user.email == "alice@test.com"
    assert user.is_active is True
    
    # 验证已加入事项
    from sqlalchemy import select

    from hub.db.models import MatterParticipant
    
    participant = db_session.execute(
        select(MatterParticipant).where(
            MatterParticipant.matter_id == test_matter.id,
            MatterParticipant.user_id == user.id,
        )
    ).scalar_one_or_none()
    assert participant is not None
    
    # 验证消费记录
    from hub.db.models import InvitationConsumption
    
    consumption = db_session.execute(
        select(InvitationConsumption).where(
            InvitationConsumption.invitation_id == invitation.id,
            InvitationConsumption.user_id == user.id,
        )
    ).scalar_one_or_none()
    assert consumption is not None
    assert consumption.ip_address == "127.0.0.1"
    assert consumption.user_agent == "TestAgent/1.0"
    
    # 验证链接状态已更新
    assert consumed_invitation.used_count == 1
    assert consumed_invitation.status == "consumed"  # max_uses=1，所以直接变为 consumed


def test_consume_invitation_link_not_found(db_session: Session):
    """测试消费不存在的链接。"""
    with pytest.raises(invitation_links.InvitationNotFoundError):
        invitation_links.consume_invitation_link(
            db=db_session,
            short_code="NOTEXIST",
            username="alice",
            email="alice@test.com",
            password_hash="hash456",
        )


def test_consume_invitation_link_duplicate_username(
    db_session: Session, test_matter: Matter, test_user: User
):
    """测试用户名已存在的情况。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    # 使用已存在的用户名
    with pytest.raises(ValueError, match="用户名.*已存在"):
        invitation_links.consume_invitation_link(
            db=db_session,
            short_code=invitation.short_code,
            username=test_user.username,  # 重复
            email="alice@test.com",
            password_hash="hash456",
        )


def test_consume_invitation_link_duplicate_email(
    db_session: Session, test_matter: Matter, test_user: User
):
    """测试邮箱已存在的情况。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    # 使用已存在的邮箱
    with pytest.raises(ValueError, match="邮箱.*已存在"):
        invitation_links.consume_invitation_link(
            db=db_session,
            short_code=invitation.short_code,
            username="alice",
            email=test_user.email,  # 重复
            password_hash="hash456",
        )


def test_revoke_invitation(db_session: Session, test_matter: Matter, test_user: User):
    """测试撤销邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.flush()
    
    # 撤销
    invitation_links.revoke_invitation(db_session, invitation.id, test_user.id)
    db_session.flush()
    
    # 验证状态
    assert invitation.status == "revoked"
    assert invitation.revoked_at is not None
    assert invitation.revoked_by == test_user.id


def test_get_matter_invitations(db_session: Session, test_matter: Matter, test_user: User):
    """测试获取事项的所有邀请链接。"""
    # 创建多个链接
    inv1 = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    inv2 = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    inv3 = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    
    # 撤销其中一个
    invitation_links.revoke_invitation(db_session, inv2.id, test_user.id)
    db_session.flush()
    
    # 默认不包含已撤销的
    invitations = invitation_links.get_matter_invitations(db_session, test_matter.id)
    assert len(invitations) == 2
    assert inv1.id in [inv.id for inv in invitations]
    assert inv3.id in [inv.id for inv in invitations]
    
    # 包含已撤销的
    all_invitations = invitation_links.get_matter_invitations(
        db_session, test_matter.id, include_revoked=True
    )
    assert len(all_invitations) == 3


def test_cleanup_expired_invitations(db_session: Session, test_matter: Matter, test_user: User):
    """测试清理过期的邀请链接。"""
    # 创建已过期的链接
    inv1 = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    inv1.expires_at = utcnow() - timedelta(days=1)
    
    # 创建未过期的链接
    inv2 = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    
    db_session.flush()
    
    # 清理
    cleaned_count = invitation_links.cleanup_expired_invitations(db_session)
    db_session.flush()
    
    assert cleaned_count == 1
    assert inv1.status == "expired"
    assert inv2.status == "active"


def test_consume_invitation_link_multiple_uses(
    db_session: Session, test_matter: Matter, test_user: User
):
    """测试可重复使用的邀请链接。"""
    # 创建可使用3次的链接
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
        max_uses=3,
    )
    db_session.commit()
    
    # 第一次消费
    user1, _ = invitation_links.consume_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
        username="alice",
        email="alice@test.com",
        password_hash="hash1",
    )
    db_session.commit()
    
    assert invitation.used_count == 1
    assert invitation.status == "active"  # 还可以继续用
    
    # 第二次消费
    user2, _ = invitation_links.consume_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
        username="bob",
        email="bob@test.com",
        password_hash="hash2",
    )
    db_session.commit()
    
    assert invitation.used_count == 2
    assert invitation.status == "active"  # 还可以继续用
    
    # 第三次消费
    user3, consumed_inv = invitation_links.consume_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
        username="charlie",
        email="charlie@test.com",
        password_hash="hash3",
    )
    db_session.commit()
    
    assert consumed_inv.used_count == 3
    assert consumed_inv.status == "consumed"  # 已用完
    
    # 第四次消费应该失败
    with pytest.raises(invitation_links.InvitationExhaustedError):
        invitation_links.consume_invitation_link(
            db=db_session,
            short_code=invitation.short_code,
            username="david",
            email="david@test.com",
            password_hash="hash4",
        )
