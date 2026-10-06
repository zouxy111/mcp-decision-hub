"""API tests for invitation links.

分两部分：
1. 业务逻辑层（``hub.domain.invitation_links``）—— 原有覆盖。
2. HTTP 路由层（``hub.web.routes_invitation``）—— 2026-10-07 补。

第2 部分是补的，因为路由层此前零覆盖，藏了两个真 bug：``matter.owner_id``
（Matter 模型里没这个字段，真名 ``initiator_id``）导致创建/列举/撤销三个
端点一调就 500，以及 JSON 端点挂在站点裸路径上。业务逻辑层测试再多也
照不到这两类问题 —— 层不同。
"""

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


# ======================================================================
# HTTP 路由层（2026-10-07 补）
# ======================================================================

# 注：下面这些用的是 test_matter，其发起人是 test_user（见上方 fixture），
# 参与人是 alice / bob。所以「发起人」视角用 test_user 登录，
# 「无关人」视角需要另建一个用户。


@pytest.fixture()
def outsider(db_session):
    """既不是事项发起人也不是参与人。"""
    return make_user(db_session, "carol", password="pw-123456")


@pytest.fixture()
def matter_of_test_user(test_matter):
    return test_matter


def _login(client, username, password="pw-123456"):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, f"登录失败：{resp.text}"
    return resp


def _create_invitation(client, matter_id, *, as_user="testuser", **payload):
    """以 ``as_user`` 身份登录并创建邀请链接。

    默认用 ``testuser``（测试事项的发起人）。需要以他人身份验证权限时传
    ``as_user``。登录放进helper 是为了每个用例不必重复写，且不会漏。
    """
    _login(client, as_user)
    body = {"matter_id": matter_id}
    body.update(payload)
    return client.post("/api/invitations/create", json=body)


# ---- POST /api/invitations/create ----


def test_HTTP_创建邀请_发起人成功(client, test_user, matter_of_test_user):
    resp = _create_invitation(client, matter_of_test_user.id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["matter_id"] == matter_of_test_user.id
    assert body["short_code"]
    assert body["full_url"] == f"/invite/{body['short_code']}"
    assert body["used_count"] == 0
    assert body["is_active"] is True


def test_HTTP_创建邀请_可指定有效期与次数上限(client, test_user, matter_of_test_user):
    resp = _create_invitation(client, matter_of_test_user.id, expires_in_days=7,
                             max_uses=3, invited_name="张经理")

    body = resp.json()
    assert body["max_uses"] == 3
    assert body["invited_name"] == "张经理"


def test_HTTP_创建邀请_非发起人被拒403(client, alice, matter_of_test_user):
    """owner_id bug 的守卫：曾经直接 AttributeError → 500。"""
    # alice 只是参与人，不是发起人
    resp = _create_invitation(client, matter_of_test_user.id, as_user="alice")

    assert resp.status_code == 403


def test_HTTP_创建邀请_事项不存在返回404(client, test_user):
    resp = _create_invitation(client, "mat-不存在")

    assert resp.status_code == 404


def test_HTTP_创建邀请_未登录被拦截(client, matter_of_test_user):
    """未登录时 ``get_current_user`` 抛 303 跳登录页。

    测试客户端默认 follow_redirects，所以看到的是登录页 HTML（200）而不是
    303 —— 断言「拿不到邀请 JSON」即可，别锁死重定向次数。
    这里不能走 _create_invitation（它会自动登录）。
    """
    resp = client.post("/api/invitations/create",
                       json={"matter_id": matter_of_test_user.id})

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "short_code" not in resp.text


def test_HTTP_创建邀请_有效期超范围返回422(client, test_user, matter_of_test_user):
    resp = _create_invitation(client, matter_of_test_user.id, expires_in_days=999)

    assert resp.status_code == 422


def test_HTTP_创建邀请_短码互不重复(client, test_user, matter_of_test_user):
    codes = {_create_invitation(client, matter_of_test_user.id).json()["short_code"]
             for _ in range(5)}

    assert len(codes) == 5


# ---- GET /api/invitations/validate/{short_code}（公开接口） ----


def test_HTTP_校验邀请_有效时返回事项信息与agent说明(client, test_user,
                                                matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    body = client.get(f"/api/invitations/validate/{code}").json()

    assert body["valid"] is True
    assert body["matter_id"] == matter_of_test_user.id
    assert body["matter_title"] == matter_of_test_user.title
    # 给对方 agent 看的接入说明
    assert body["agent_brief"]


def test_HTTP_校验邀请_不存在返回valid_false(client):
    body = client.get("/api/invitations/validate/xxxxxx").json()

    assert body["valid"] is False
    assert body["error"]


def test_HTTP_校验邀请_受邀姓名回显(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id,
                             invited_name="李主任").json()["short_code"]

    assert client.get(
        f"/api/invitations/validate/{code}").json()["invited_name"] == "李主任"


# ---- POST /api/invitations/consume/{short_code}（公开） ----


def test_HTTP_消费邀请_注册成功且可登录(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "dave", "email": "dave@example.com",
        "password": "dave-pw-123", "display_name": "Dave",
        "responsibility": "负责后端",
    })

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["user_id"]
    assert client.post("/api/auth/login",
                       json={"username": "dave",
                             "password": "dave-pw-123"}).status_code == 200


def test_HTTP_消费邀请_自我介绍必填(client, test_user, matter_of_test_user):
    """留言板形态要求先自我介绍，接口与页面同口径。"""
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "dave", "email": "dave@example.com",
        "password": "dave-pw-123",
    })

    assert resp.status_code == 422


def test_HTTP_消费邀请_用户名重复返回错误(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "alice", "email": "other@example.com",
        "password": "pwd-123456", "display_name": "同名",
        "responsibility": "负责测试",
    })

    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["error"]


def test_HTTP_消费邀请_邮箱格式非法返回422(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "dave", "email": "不是邮箱",
        "password": "dave-pw-123", "display_name": "Dave",
        "responsibility": "负责后端",
    })

    assert resp.status_code == 422


def test_HTTP_消费邀请_密码过短返回422(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id).json()["short_code"]

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "dave", "email": "dave@example.com",
        "password": "123", "display_name": "Dave",
        "responsibility": "负责后端",
    })

    assert resp.status_code == 422


def test_HTTP_消费邀请_超次数上限后失效(client, test_user, matter_of_test_user):
    code = _create_invitation(client, matter_of_test_user.id,
                             max_uses=1).json()["short_code"]
    client.post(f"/api/invitations/consume/{code}", json={
        "username": "dave", "email": "dave@example.com",
        "password": "dave-pw-123", "display_name": "Dave",
        "responsibility": "负责后端",
    })

    resp = client.post(f"/api/invitations/consume/{code}", json={
        "username": "erin", "email": "erin@example.com",
        "password": "erin-pw-123", "display_name": "Erin",
        "responsibility": "负责前端",
    })

    assert resp.json()["success"] is False


# ---- GET /api/invitations/matter/{matter_id}（owner_id bug 第二处） ----


def test_HTTP_列举事项邀请_发起人成功(client, test_user, matter_of_test_user):
    _create_invitation(client, matter_of_test_user.id, invited_name="A")
    _create_invitation(client, matter_of_test_user.id, invited_name="B")

    resp = client.get(f"/api/invitations/matter/{matter_of_test_user.id}")

    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_HTTP_列举事项邀请_非发起人被拒403(client, alice, matter_of_test_user):
    # 借helper 登录（alice 是参与人，非发起人）
    _create_invitation(client, matter_of_test_user.id, as_user="alice")

    resp = client.get(f"/api/invitations/matter/{matter_of_test_user.id}")

    assert resp.status_code == 403


def test_HTTP_列举事项邀请_可含已失效邀请(client, test_user, matter_of_test_user):
    inv_id = _create_invitation(
        client, matter_of_test_user.id).json()["id"]
    client.post(f"/api/invitations/{inv_id}/revoke")

    active_only = client.get(
        f"/api/invitations/matter/{matter_of_test_user.id}").json()
    with_inactive = client.get(
        f"/api/invitations/matter/"
        f"{matter_of_test_user.id}?include_inactive=true").json()

    assert len(active_only) == 0
    assert len(with_inactive) == 1


def test_HTTP_列举事项邀请_事项不存在返回404(client, test_user):
    _login(client, "testuser")

    resp = client.get("/api/invitations/matter/mat-不存在")

    assert resp.status_code == 404


# ---- POST /api/invitations/{id}/revoke（owner_id bug 第三处） ----


def test_HTTP_撤销邀请_发起人成功(client, test_user, matter_of_test_user):
    inv_id = _create_invitation(
        client, matter_of_test_user.id).json()["id"]

    resp = client.post(f"/api/invitations/{inv_id}/revoke")

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_HTTP_撤销邀请_无关人403(client, outsider, test_user, matter_of_test_user):
    # 先以发起人身份建链接（helper 自动登录 testuser）
    inv_id = _create_invitation(
        client, matter_of_test_user.id).json()["id"]
    # 再切到无关人视角
    _login(client, "carol")

    resp = client.post(f"/api/invitations/{inv_id}/revoke")

    assert resp.status_code == 403


def test_HTTP_撤销邀请_不存在返回404(client, test_user):
    _login(client, "testuser")

    resp = client.post("/api/invitations/inv-不存在/revoke")

    assert resp.status_code == 404


# ---- 路由前缀守卫 ----


def test_HTTP_JSON端点均带api前缀(client):
    """2026-10-07 前 JSON 端点用裸路径（POST /create 等），已改前缀。

    这条防止有人把新端点又加回裸路径 —— 挂根上的 ``/create`` 既难读，
    又容易和别的路由撞名。
    """
    spec = client.app.openapi()

    bare = [p for p in spec["paths"]
            if p in ("/create", "/consume/{short_code}", "/{invitation_id}/revoke")]
    assert bare == [], f"JSON 端点漏了 /api 前缀：{bare}"

    # 落地页仍在根上（外部人点邮件里的链接要用）
    assert "/invite/{short_code}" in spec["paths"]
    assert "/api/invitations/create" in spec["paths"]
