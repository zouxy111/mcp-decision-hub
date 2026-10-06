"""测试邀请链接的 HTML 页面路由。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hub.db.models import Matter, MatterParticipant, User
from hub.domain import invitation_links
from hub.main import create_app


@pytest.fixture
def app(settings):
    """创建测试应用。"""
    return create_app(settings)


def test_show_invitation_page_valid(app, db_session):
    """测试显示有效的邀请页面。"""
    # 准备测试数据
    user = User(
        username="testowner",
        email="owner@test.com",
        password_hash="hashed_password",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    
    matter = Matter(
        title="测试事项",
        goal="测试目标",
        initiator_id=user.id,
        status="draft",
    )
    db_session.add(matter)
    db_session.commit()
    db_session.refresh(matter)
    
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=matter.id,
        created_by=user.id,
        expires_in_days=7,
        max_uses=1,
        invited_name="测试用户",
    )
    db_session.commit()
    db_session.refresh(invitation)
    
    # 测试页面
    with TestClient(app) as client:
        response = client.get(f"/invite/{invitation.short_code}")
    
        assert response.status_code == 200
        assert "加入协作" in response.text
        assert "测试事项" in response.text
        assert "testowner" in response.text


def test_show_invitation_page_invalid(app, db_session):
    """测试显示无效的邀请页面。"""
    with TestClient(app) as client:
        response = client.get("/invite/INVALID")
    
        assert response.status_code == 200
        assert "邀请链接无效" in response.text or "Invitation not found" in response.text


def test_accept_invitation_success(app, db_session):
    """测试成功接受邀请。"""
    # 准备测试数据
    user = User(
        username="testowner",
        email="owner@test.com",
        password_hash="hashed_password",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    
    matter = Matter(
        title="测试事项",
        goal="测试目标",
        initiator_id=user.id,
        status="draft",
    )
    db_session.add(matter)
    db_session.commit()
    db_session.refresh(matter)
    
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=matter.id,
        created_by=user.id,
        expires_in_days=7,
        max_uses=1,
        invited_name="新用户",
    )
    db_session.commit()
    db_session.refresh(invitation)
    
    # 测试接受邀请
    with TestClient(app) as client:
        # 先获取 CSRF token
        page_response = client.get(f"/invite/{invitation.short_code}")
        assert page_response.status_code == 200
        
        # 提交表单
        response = client.post(
            f"/invite/{invitation.short_code}/accept",
            data={
                "username": "newuser",
                "email": "new@test.com",
                "password": "password123",
                "password_confirm": "password123",
                # 2026-10-04 起：先自我介绍再建账号，两项必填
                "display_name": "新用户",
                "responsibility": "负责测试这条链路",
            },
            follow_redirects=False,
        )
    
        # 应该重定向到登录页面或仪表板
        assert response.status_code in [302, 303]

    # 姓名 / 负责内容要落到「事项 × 参与人」名片上（留言板按这个显示谁在说话）
    created = db_session.scalar(
        select(User).where(User.username == "newuser"))
    card = db_session.get(MatterParticipant, (matter.id, created.id))
    assert card is not None
    assert card.display_name == "新用户"
    assert card.responsibility == "负责测试这条链路"


def test_accept_invitation_requires_self_intro(app, db_session):
    """留言板形态：不填姓名 / 负责内容就直接打回来，不建账号。"""
    owner = User(username="owner2", email="owner2@test.com",
                 password_hash="hashed_password")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    matter = Matter(title="测试事项2", goal="目标", initiator_id=owner.id,
                    status="open", mode="board")
    db_session.add(matter)
    db_session.commit()
    invitation = invitation_links.create_invitation_link(
        db=db_session, matter_id=matter.id, created_by=owner.id,
        expires_in_days=7, max_uses=1,
    )
    db_session.commit()

    with TestClient(app) as client:
        response = client.post(
            f"/invite/{invitation.short_code}/accept",
            data={
                "username": "nobody",
                "email": "nobody@test.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )

    assert response.status_code == 400
    assert "姓名" in response.text
    assert db_session.scalar(select(User).where(User.username == "nobody")) is None


def test_accept_invitation_password_mismatch(app, db_session):
    """测试密码不匹配。"""
    # 准备测试数据
    user = User(
        username="testowner",
        email="owner@test.com",
        password_hash="hashed_password",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    
    matter = Matter(
        title="测试事项",
        goal="测试目标",
        initiator_id=user.id,
        status="draft",
    )
    db_session.add(matter)
    db_session.commit()
    db_session.refresh(matter)
    
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=matter.id,
        created_by=user.id,
        expires_in_days=7,
        max_uses=1,
    )
    db_session.commit()
    db_session.refresh(invitation)
    
    with TestClient(app) as client:
        response = client.post(
            f"/invite/{invitation.short_code}/accept",
            data={
                "username": "newuser",
                "email": "new@test.com",
                "password": "password123",
                "password_confirm": "different",
                "display_name": "新用户",
                "responsibility": "负责测试",
            },
        )
    
        assert response.status_code in [200, 400]
        assert "密码不匹配" in response.text or "不一致" in response.text


def test_accept_invitation_invalid_code(app, db_session):
    """测试使用无效的邀请码。"""
    with TestClient(app) as client:
        response = client.post(
            "/invite/INVALID/accept",
            data={
                "username": "newuser",
                "email": "new@test.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )
    
        assert response.status_code in [200, 400]
        assert "无效" in response.text or "不存在" in response.text
