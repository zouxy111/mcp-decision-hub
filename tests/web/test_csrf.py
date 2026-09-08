"""CSRF protection tests (P2 Web 端 CSRF 防护).

使用原生 TestClient（不走 conftest 的自动注入），手工构造 token 验证
同步器校验：缺失/伪造/跨会话一律 403，合法 token 放行。
"""

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

from hub.main import create_app
from tests.conftest import make_user


@pytest.fixture()
def app(settings):
    return create_app(settings)


def _login(client, username, password="pw-123456"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def _token(client, secret="test-secret"):
    sid = client.cookies.get("hub_session")
    return URLSafeSerializer(secret, salt="hub-csrf").dumps({"sid": sid})


def _new_matter(client, **data):
    payload = {"title": "T", "goal": "G", "background": "B",
               "participants": "alice,bob", "draft_questions": "Q1?"}
    payload.update(data)
    return client.post("/matters/new", data=payload, follow_redirects=False)


def test_missing_token_rejected(app, db_session):
    make_user(db_session, "init", password="pw-123456")
    db_session.commit()
    with TestClient(app) as client:
        _login(client, "init")
        resp = _new_matter(client)  # 不带 csrf_token
        assert resp.status_code == 403
        assert "CSRF" in resp.text


def test_forged_token_rejected(app, db_session):
    make_user(db_session, "init", password="pw-123456")
    db_session.commit()
    with TestClient(app) as client:
        _login(client, "init")
        resp = _new_matter(client, csrf_token="hacked-token")
        assert resp.status_code == 403
        # 伪造签名（知道 salt 但 sid 不匹配当前会话）
        forged = URLSafeSerializer("test-secret", salt="hub-csrf").dumps(
            {"sid": "attacker-session"})
        resp = _new_matter(client, csrf_token=forged)
        assert resp.status_code == 403


def test_valid_token_accepted(app, db_session):
    make_user(db_session, "init", password="pw-123456")
    make_user(db_session, "alice", password="pw-123456")
    make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    with TestClient(app) as client:
        _login(client, "init")
        resp = _new_matter(client, csrf_token=_token(client))
        assert resp.status_code in (303, 200)
        assert resp.status_code != 403


def test_token_bound_to_session(app, db_session):
    make_user(db_session, "u1", password="pw-123456")
    make_user(db_session, "u2", password="pw-123456")
    db_session.commit()
    with TestClient(app) as client:
        _login(client, "u1")
        token_u1 = _token(client)
        # 同一浏览器重新登录 u2（会话 Cookie 变化），u1 的 token 不再有效
        _login(client, "u2")
        resp = _new_matter(client, csrf_token=token_u1)
        assert resp.status_code == 403


def test_logout_requires_token(app, db_session):
    make_user(db_session, "init", password="pw-123456")
    db_session.commit()
    with TestClient(app) as client:
        _login(client, "init")
        assert client.post("/logout", follow_redirects=False).status_code == 403
        resp = client.post("/logout", data={"csrf_token": _token(client)},
                           follow_redirects=False)
        assert resp.status_code == 303
