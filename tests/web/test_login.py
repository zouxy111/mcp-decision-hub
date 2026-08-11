from tests.conftest import make_user


def _login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_login_page_renders_llm_notice(client, settings):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "第三方大模型服务商" in resp.text
    assert settings.llm_provider_name in resp.text
    assert "骨架阶段" in resp.text


def test_wrong_password_shows_generic_error(client, db_session):
    make_user(db_session, "alice", password="right-pw-1")
    db_session.commit()
    resp = _login(client, "alice", "wrong-pw-1")
    assert resp.status_code == 200
    assert "用户名或密码错误" in resp.text


def test_login_success_sets_cookie_and_redirects(client, db_session):
    make_user(db_session, "alice", password="right-pw-1")
    db_session.commit()
    resp = _login(client, "alice", "right-pw-1")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "hub_session" in resp.headers["set-cookie"]


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_must_change_password_forces_redirect(client, db_session):
    make_user(db_session, "root", password="init-pw-123", must_change_password=True)
    db_session.commit()
    resp = _login(client, "root", "init-pw-123")
    assert resp.headers["location"] == "/change-password"
    # even direct dashboard access bounces to change-password
    resp2 = client.get("/dashboard", follow_redirects=False)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/change-password"


def test_change_password_flow(client, db_session):
    make_user(db_session, "root", password="init-pw-123", must_change_password=True)
    db_session.commit()
    _login(client, "root", "init-pw-123")
    resp = client.post(
        "/change-password",
        data={"new_password": "brand-new-pw", "confirm_password": "brand-new-pw"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    resp2 = client.get("/dashboard")
    assert resp2.status_code == 200


def test_change_password_mismatch_rerenders(client, db_session):
    make_user(db_session, "root", password="init-pw-123", must_change_password=True)
    db_session.commit()
    _login(client, "root", "init-pw-123")
    resp = client.post(
        "/change-password",
        data={"new_password": "abc-12345", "confirm_password": "different"},
    )
    assert resp.status_code == 200
    assert "两次输入的密码不一致" in resp.text


def test_invite_consume_via_login_page(client, db_session):
    from hub.api.accounts import create_invitation

    admin = make_user(db_session, "admin", is_admin=True)
    _, invite_token = create_invitation(
        db_session, admin=admin, username="newbie", email="n@x.com", ttl_seconds=3600
    )
    db_session.commit()
    resp = client.post(
        "/invite/consume",
        data={"username": "newbie", "invitation_token": invite_token,
              "new_password": "my-new-pw-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_invite_consume_invalid_generic_error(client, db_session):
    resp = client.post(
        "/invite/consume",
        data={"username": "ghost", "invitation_token": "nope",
              "new_password": "my-new-pw-1"},
    )
    assert resp.status_code == 200
    assert "邀请凭证无效或已过期" in resp.text


def test_logout_clears_session(client, db_session):
    make_user(db_session, "alice", password="right-pw-1")
    db_session.commit()
    _login(client, "alice", "right-pw-1")
    client.post("/logout", follow_redirects=False)
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.headers["location"] == "/login"
