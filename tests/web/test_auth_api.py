"""web-ui 前端 JSON 认证接口测试（``hub/web/routes_auth_api.py``）。

覆盖：登录成功/失败/限流、Cookie 会话、``/session`` 探测、token 签发、
改密码、以及「与既有 HTML 登录通道共享 Cookie 会话」这条关键契约。

新增接口不应改变既有行为：876 个既有测试在加入本模块前后同样全绿。
"""

import pytest

from tests.conftest import make_user


@pytest.fixture()
def login_user(db_session):
    return make_user(db_session, "alice", "pw-12345")


# --------------------------------------------------------------------------
# POST /api/auth/login
# --------------------------------------------------------------------------


def test_登录成功_返回用户信息并种下Cookie(client, login_user):
    resp = client.post("/api/auth/login",
                       json={"username": "alice", "password": "pw-12345"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["is_admin"] is False
    assert body["must_change_password"] is False
    # Cookie 登录不发 token，前端靠 Cookie 维持登录态
    assert body["access_token"] is None
    assert "hub_session" in client.cookies


def test_登录成功_审计记录写入JSON通道(client, db_session, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    from hub.api import audit
    from hub.db.models import AuditEvent

    rows = db_session.query(AuditEvent).filter(
        AuditEvent.event_type == audit.LOGIN_SUCCESS).all()
    assert len(rows) == 1


def test_密码错误_返回401且不种Cookie(client, login_user):
    resp = client.post("/api/auth/login",
                       json={"username": "alice", "password": "wrong-pw"})

    assert resp.status_code == 401
    assert "hub_session" not in client.cookies


def test_用户不存在_同样返回401不泄露用户是否存在(client, db_session):
    resp = client.post("/api/auth/login",
                       json={"username": "nobody", "password": "pw-12345"})

    assert resp.status_code == 401


def test_停用用户_无法登录(client, db_session):
    make_user(db_session, "bob", "pw-12345", is_active=False)

    resp = client.post("/api/auth/login",
                       json={"username": "bob", "password": "pw-12345"})

    assert resp.status_code == 401


def test_待激活账号_无法登录(client, db_session, login_user):
    """受邀未设密码的账号用 marker 占位，不接受任何密码登录。"""
    from hub.api.accounts import INVITED_PASSWORD_MARKER
    from hub.db.models import User

    login_user.password_hash = INVITED_PASSWORD_MARKER
    db_session.flush()

    resp = client.post("/api/auth/login",
                       json={"username": "alice", "password": "pw-12345"})

    assert resp.status_code == 401


def test_缺少字段_返回422(client, login_user):
    resp = client.post("/api/auth/login", json={"username": "alice"})

    assert resp.status_code == 422


def test_多余字段_返回422(client, login_user):
    """extra=forbid：客户端不能注入 role 之类的字段。"""
    resp = client.post("/api/auth/login",
                       json={"username": "alice", "password": "pw-12345",
                             "is_admin": True})

    assert resp.status_code == 422


def test_强制改密账号_登录成功但标记为待改密(client, db_session):
    make_user(db_session, "carol", "pw-12345", must_change_password=True)

    resp = client.post("/api/auth/login",
                       json={"username": "carol", "password": "pw-12345"})

    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


# --------------------------------------------------------------------------
# 限流（与 HTML 登录共用 username + IP 双维度）
# --------------------------------------------------------------------------


def test_登录限流_超限后返回429带RetryAfter(client, settings, login_user):
    limiter = client.app.state.limiter
    from hub.domain.rate_limit import rate_limit_key_login_username

    key = rate_limit_key_login_username("alice")
    limit = settings.rate_limit_login_username_per_minute
    for _ in range(limit):
        limiter.allow(key, limit=limit)

    resp = client.post("/api/auth/login",
                       json={"username": "alice", "password": "pw-12345"})

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --------------------------------------------------------------------------
# GET /api/auth/session 与 /api/auth/me
# --------------------------------------------------------------------------


def test_session探测_未登录返回authenticated_false(client):
    resp = client.get("/api/auth/session")

    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False
    assert resp.json()["user"] is None


def test_session探测_已登录返回用户信息(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    resp = client.get("/api/auth/session")

    assert resp.json()["authenticated"] is True
    assert resp.json()["user"]["username"] == "alice"


def test_me_未登录重定向而非401(client):
    """复用 get_current_user，未登录时是 303跳登录页（前端据此跳页）。"""
    resp = client.get("/api/auth/me", follow_redirects=False)

    assert resp.status_code == 303


def test_me_已登录返回当前用户(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    resp = client.get("/api/auth/me")

    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


# --------------------------------------------------------------------------
# POST /api/auth/token —— 供前端调既有 /api/*（Bearer 通道）
# --------------------------------------------------------------------------


def test_签发token_返回明文且库里只存哈希(client, db_session, login_user):
    from hub.db.models import AgentToken

    resp = client.post("/api/auth/token",
                       json={"username": "alice", "password": "pw-12345"})

    assert resp.status_code == 200
    body = resp.json()
    plaintext = body["access_token"]
    assert plaintext and plaintext.startswith("hdt_")
    assert body["token_name"] == "web-ui"

    #库里存的是 SHA-256，不是明文
    row = db_session.query(AgentToken).one()
    assert row.token_hash != plaintext
    assert len(row.token_hash) == 64  # sha256 hex


def test_签发的token可调用既有API(client, login_user):
    """这是本模块存在的核心原因：前端拿到的 token 能直接用 /api/items。"""
    token = client.post("/api/auth/token",
                        json={"username": "alice",
                              "password": "pw-12345"}).json()["access_token"]

    resp = client.get("/api/items", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200


def test_签发token_密码错误不发token(client, login_user):
    resp = client.post("/api/auth/token",
                       json={"username": "alice", "password": "wrong"})

    assert resp.status_code == 401
    assert client.cookies.get("hub_session") is None


# --------------------------------------------------------------------------
# POST /api/auth/logout 与 /api/auth/change-password
# --------------------------------------------------------------------------


def test_登出_清除Cookie(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})
    assert client.cookies.get("hub_session") is not None

    resp = client.post("/api/auth/logout")

    assert resp.status_code == 200
    assert not client.cookies.get("hub_session")


def test_改密_成功后可以用新密码登录(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    resp = client.post("/api/auth/change-password",
                       json={"new_password": "new-pw-123456",
                             "confirm_password": "new-pw-123456"})

    assert resp.status_code == 200
    client.post("/api/auth/logout")
    again = client.post("/api/auth/login",
                        json={"username": "alice", "password": "new-pw-123456"})
    assert again.status_code == 200


def test_改密_两次不一致返回400(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    resp = client.post("/api/auth/change-password",
                       json={"new_password": "new-pw-123456",
                             "confirm_password": "different-pw"})

    assert resp.status_code == 400


def test_改密_少于8位返回400(client, login_user):
    client.post("/api/auth/login",
                json={"username": "alice", "password": "pw-12345"})

    resp = client.post("/api/auth/change-password",
                       json={"new_password": "short", "confirm_password": "short"})

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# 与 HTML 登录通道的契约：共享同一个 hub_session Cookie
# --------------------------------------------------------------------------


def test_HTML登录后前端自动是登录态(client, login_user):
    """用户在Jinja2 页面登录后，同域的 React 前端不需要再登录一次。"""
    client.post("/login", data={"username": "alice", "password": "pw-12345"},
                follow_redirects=False)

    resp = client.get("/api/auth/session")

    assert resp.json()["authenticated"] is True
    assert resp.json()["user"]["username"] == "alice"