"""邀请链接 v23 + 管理闭环测试（2026-10-12）。

覆盖：永不过期、按人绑定（account_exists）、Agent 零页面（/invite 内容
协商）、落地页登录入板（login_join）、移除参与人、发起人管理页、
MCP 三工具（建/查/吊销）。
"""

import pytest

from hub.api import matters as matter_svc
from hub.domain import invitation_links
from tests.conftest import make_user


@pytest.fixture()
def owner(db_session):
    return make_user(db_session, "owner", "pw-123456")


@pytest.fixture()
def yangqi(db_session):
    return make_user(db_session, "yangqi", "pw-123456")


@pytest.fixture()
def board(db_session, owner):
    return matter_svc.create_board_matter(
        db_session, initiator=owner, title="整改协作", goal="G",
        participant_ids=[])


def _login(client, username, password="pw-123456"):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# A 永不过期 / B 按人绑定
# --------------------------------------------------------------------------

def test_创建永久链接_不过期(db_session, owner, board):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        expires_in_days=None, max_uses=None)
    assert inv.expires_at is None
    result = invitation_links.validate_invitation_link(db_session, inv.short_code)
    assert result["valid"] is True
    assert result["expires_at"] is None


def test_绑定用户名_validate返回账号状态(db_session, owner, board, yangqi):
    inv_new = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="nobody_yet")
    inv_old = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="yangqi")
    db_session.commit()
    r1 = invitation_links.validate_invitation_link(db_session, inv_new.short_code)
    r2 = invitation_links.validate_invitation_link(db_session, inv_old.short_code)
    assert r1["account_exists"] is False
    assert r2["account_exists"] is True


# --------------------------------------------------------------------------
# C Agent 零页面：/invite 直接返回说明文本
# --------------------------------------------------------------------------

def test_Agent打开链接直接拿说明文本(client, db_session, owner, board, yangqi):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="yangqi")
    db_session.commit()

    r = client.get(f"/invite/{inv.short_code}?for_agent=1")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "只要密码" in r.text  # account_exists=True → 登录入板分支
    assert "本人已有账号" in r.text


def test_通用链接_brief保持双路径(client, db_session, owner, board):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id)
    db_session.commit()
    r = client.get(f"/invite/{inv.short_code}?for_agent=1")
    assert "用户名 … 已存在" in r.text  # 通用回退提示


def test_落地页_已绑定账号显示密码框(client, db_session, owner, board, yangqi):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_name="杨琦", invited_username="yangqi")
    db_session.commit()
    page = client.get(f"/invite/{inv.short_code}")
    assert "登录并加入协作" in page.text
    assert "创建您的账号" not in page.text


def test_落地页_未注册显示预填注册表单(client, db_session, owner, board):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="new_person")
    db_session.commit()
    page = client.get(f"/invite/{inv.short_code}")
    assert 'value="new_person"' in page.text
    assert "创建您的账号" in page.text


# --------------------------------------------------------------------------
# D login_join：有账号输密码直接进板
# --------------------------------------------------------------------------

def test_login_join_密码正确直接入板(client, db_session, owner, board, yangqi):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="yangqi")
    db_session.commit()

    r = client.post(f"/invite/{inv.short_code}/login_join",
                    data={"username": "yangqi", "password": "pw-123456"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/matters/{board.id}"
    assert "hub_session" in r.headers.get("set-cookie", "")
    assert matter_svc.is_participant(db_session, matter_id=board.id,
                                     user_id=yangqi.id)


def test_login_join_密码错误被拦(client, db_session, owner, board, yangqi):
    inv = invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_username="yangqi")
    db_session.commit()
    r = client.post(f"/invite/{inv.short_code}/login_join",
                    data={"username": "yangqi", "password": "wrong-pw"})
    assert "密码不对" in r.text
    assert not matter_svc.is_participant(db_session, matter_id=board.id,
                                         user_id=yangqi.id)


# --------------------------------------------------------------------------
# E 移除参与人
# --------------------------------------------------------------------------

def test_发起人可移除参与人_被移除者失去访问权(client, db_session, owner,
                                              board, yangqi):
    matter_svc.create_board_matter  # noqa - 保持导入
    db_session.add(__import__("hub.db.models", fromlist=["MatterParticipant"])
                   .MatterParticipant(matter_id=board.id, user_id=yangqi.id))
    db_session.commit()

    _login(client, "owner")
    r = client.post(f"/matters/{board.id}/participants/{yangqi.id}/remove",
                    follow_redirects=False)
    assert r.status_code == 303
    assert not matter_svc.is_participant(db_session, matter_id=board.id,
                                         user_id=yangqi.id)
    # 被移除者读板 → 404
    _login(client, "yangqi")
    r = client.get(f"/matters/{board.id}")
    assert r.status_code == 404


def test_非发起人不能移除参与人(client, db_session, owner, board, yangqi):
    from hub.db.models import MatterParticipant
    db_session.add(MatterParticipant(matter_id=board.id, user_id=yangqi.id))
    db_session.commit()
    _login(client, "yangqi")
    r = client.post(f"/matters/{board.id}/participants/{owner.id}/remove")
    assert r.status_code == 403


# --------------------------------------------------------------------------
# F 发起人邀请管理页
# --------------------------------------------------------------------------

def test_管理页_发起人可看_非发起人403(client, db_session, owner, board, yangqi):
    invitation_links.create_invitation_link(
        db=db_session, matter_id=board.id, created_by=owner.id,
        invited_name="杨琦")
    db_session.commit()

    _login(client, "owner")
    page = client.get(f"/matters/{board.id}/invitations")
    assert page.status_code == 200 and "杨琦" in page.text

    _login(client, "yangqi")
    assert client.get(f"/matters/{board.id}/invitations").status_code == 403


def test_管理页_创建表单生成永久链接(client, db_session, owner, board):
    _login(client, "owner")
    r = client.post(f"/matters/{board.id}/invitations",
                    data={"invited_name": "张三", "invited_username": "zhangsan",
                          "expires_in_days": "", "max_uses": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    inv = db_session.scalar(
        __import__("sqlalchemy").select(invitation_links.InvitationLink))
    assert inv.invited_username == "zhangsan"
    assert inv.expires_at is None


# --------------------------------------------------------------------------
# G MCP 三工具
# --------------------------------------------------------------------------

def test_MCP_邀请三工具_发起人全流程(db_session, owner, board, yangqi):
    from hub.config import load_settings
    from hub.mcp_server import methods as m

    settings = load_settings()
    created = m.mcp_create_invitation(
        db_session, settings, user_id=owner.id, matter_id=board.id,
        invited_name="杨琦", invited_username="yangqi")
    assert created["short_code"] and "永久" or created["expires_at"] is None

    listed = m.mcp_list_invitations(db_session, settings, user_id=owner.id,
                                    matter_id=board.id)
    assert listed["invitations"][0]["invited_name"] == "杨琦"

    revoked = m.mcp_revoke_invitation(db_session, settings, user_id=owner.id,
                                      invitation_id=created["invitation_id"])
    assert revoked["success"] is True
    r = invitation_links.validate_invitation_link(db_session,
                                                  created["short_code"])
    assert r["valid"] is False


def test_MCP_非发起人建链接被拒(db_session, owner, board, yangqi):
    from hub.api.errors import ApiError
    from hub.config import load_settings
    from hub.mcp_server import methods as m

    with pytest.raises(ApiError) as exc:
        m.mcp_create_invitation(db_session, load_settings(), user_id=yangqi.id,
                                matter_id=board.id, invited_name="x")
    assert exc.value.status_code == 403
