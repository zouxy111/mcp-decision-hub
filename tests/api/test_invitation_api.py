"""API tests for invitation links."""

import pytest

from hub.api import matters
from hub.api.passwords import hash_password
from hub.domain import invitation_links
from tests.conftest import make_user


@pytest.fixture()
def test_user(db_session):
    """Create a test user."""
    return make_user(db_session, "testuser", password="pw-123456")


@pytest.fixture()
def alice(db_session):
    """Create Alice user."""
    return make_user(db_session, "alice", password="pw-123456")


@pytest.fixture()
def bob(db_session):
    """Create Bob user."""
    return make_user(db_session, "bob", password="pw-123456")


@pytest.fixture()
def test_matter(db_session, test_user, alice, bob):
    """Create a test matter."""
    matter = matters.create_matter(
        db_session,
        initiator=test_user,
        title="Test Matter",
        goal="Test Goal",
        background="Test Background",
        participant_ids=[alice.id, bob.id],
        initiator_participates=False,
        timeout_seconds=3600,
        max_rounds=5,
        draft_questions=["Question 1?"],
    )
    db_session.flush()
    return matter


def test_create_invitation_link_business_logic(db_session, test_user, test_matter):
    """测试业务逻辑层创建邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    assert invitation.matter_id == test_matter.id
    assert invitation.created_by == test_user.id
    assert len(invitation.short_code) == 6
    assert invitation.status == "active"
    assert invitation.used_count == 0


def test_validate_invitation_link(db_session, test_user, test_matter):
    """测试验证邀请链接。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    result = invitation_links.validate_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
    )
    
    assert result["valid"] is True
    assert result["matter_id"] == test_matter.id
    assert result["matter_title"] == test_matter.title


def test_consume_invitation_link_new_user(db_session, test_user, test_matter):
    """测试新用户使用邀请链接注册。"""
    invitation = invitation_links.create_invitation_link(
        db=db_session,
        matter_id=test_matter.id,
        created_by=test_user.id,
    )
    db_session.commit()
    
    password_hash = hash_password("password123")
    
    user, invitation_used = invitation_links.consume_invitation_link(
        db=db_session,
        short_code=invitation.short_code,
        username="newuser123",
        email="newuser@example.com",
        password_hash=password_hash,
    )
    
    assert user.username == "newuser123"
    assert user.email == "newuser@example.com"
    assert invitation_used.matter_id == test_matter.id
    
    db_session.refresh(invitation)
    assert invitation.used_count == 1
