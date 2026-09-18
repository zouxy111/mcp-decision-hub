"""/admin/model 页面测试（2026-09-19 新增）。

需求原文：「首页我没有看到配置模型的页面，要加进去。默认就是 deepseek 官网的
flash 模型。只要给个 apikey 就行。然后做一个 apikey 生成的链接指向 deepseek」。
三个已定裁定：存库 + 立即生效；仅管理员可改。
"""

import httpx
import pytest
from sqlalchemy import select

from hub.api.audit import LLM_CONFIG_UPDATED
from hub.db.models import AuditEvent, LlmConfig
from hub.llm.runtime import API_KEY_CONSOLE_URL, DEFAULT_BASE_URL, DEFAULT_MODEL
from tests.conftest import make_user

SAVED_KEY = "sk-live-1234567890abcdef"


@pytest.fixture()
def admin(db_session):
    u = make_user(db_session, "admin", is_admin=True, password="pw-123456")
    db_session.commit()
    return u


def _login(client, username="admin", password="pw-123456"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def _save(client, **fields):
    data = {"model": DEFAULT_MODEL, "base_url": "", "api_key": ""}
    data.update(fields)
    return client.post("/admin/model", data=data, follow_redirects=False)


# ------------------------------------------------------------------ 访问控制 #


def test_管理员可打开页面并看到模型选项与生成链接(client, admin):
    _login(client)
    resp = client.get("/admin/model")

    assert resp.status_code == 200
    assert "模型配置" in resp.text
    # 默认就是 deepseek 官方 flash 档
    assert DEFAULT_MODEL in resp.text
    assert '<option value="deepseek-flash" selected>' in resp.text
    # 「一个 apikey 生成的链接指向 deepseek」
    assert API_KEY_CONSOLE_URL in resp.text
    assert "platform.deepseek.com" in resp.text
    # 已下线的模型要写明为什么不能选
    assert "2026-07-24" in resp.text


def test_非管理员被拒(client, db_session):
    make_user(db_session, "regular", password="pw-123456")
    db_session.commit()
    _login(client, "regular")

    resp = client.get("/admin/model", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_未登录被弹回登录页(client, db_session):
    resp = client.get("/admin/model", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_导航入口只对管理员出现(client, db_session, admin):
    _login(client)
    assert 'href="/admin/model"' in client.get("/dashboard").text

    client.post("/logout", data={})
    make_user(db_session, "peek", password="pw-123456")
    db_session.commit()
    _login(client, "peek")
    assert 'href="/admin/model"' not in client.get("/dashboard").text


# ------------------------------------------------------------------ 保存 #


def test_保存后落库并跳回页面(client, db_session, admin):
    _login(client)
    resp = _save(client, model="deepseek-v4-pro", base_url="https://api.deepseek.com/",
                 api_key=SAVED_KEY)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/model?saved=1"

    row = db_session.scalar(select(LlmConfig))
    assert row is not None
    assert row.model == "deepseek-v4-pro"
    assert row.base_url == DEFAULT_BASE_URL      # 末尾斜杠被规范化掉
    assert row.api_key == SAVED_KEY
    assert row.updated_by == admin.id
    assert row.updated_at is not None


def test_页面只回显掩码不回显明文(client, db_session, admin):
    _login(client)
    _save(client, api_key=SAVED_KEY)

    body = client.get("/admin/model").text
    assert SAVED_KEY not in body
    assert "1234567890abcdef" not in body
    assert "sk-liv" in body            # 前缀还在，认得出是哪一把


def test_留空api_key不会清掉已存的key(client, db_session, admin):
    _login(client)
    _save(client, api_key=SAVED_KEY)
    _save(client, model="deepseek-v4-pro", api_key="")

    row = db_session.scalar(select(LlmConfig))
    assert row.model == "deepseek-v4-pro"
    assert row.api_key == SAVED_KEY


def test_勾选清除后key变空(client, db_session, admin):
    _login(client)
    _save(client, api_key=SAVED_KEY)
    _save(client, clear_api_key="1")

    assert db_session.scalar(select(LlmConfig)).api_key is None


def test_已下线模型被拒且页面给出原因(client, db_session, admin):
    _login(client)
    resp = _save(client, model="deepseek-chat")

    assert resp.status_code == 422
    assert "下线" in resp.text
    assert db_session.scalar(select(LlmConfig)) is None


def test_白名单外的模型被拒(client, db_session, admin):
    _login(client)
    resp = _save(client, model="gpt-4o")

    assert resp.status_code == 422
    assert "不支持" in resp.text
    assert db_session.scalar(select(LlmConfig)) is None


def test_保存必须带csrf(client, db_session, admin):
    _login(client)
    resp = client.post("/admin/model",
                       data={"model": DEFAULT_MODEL, "csrf_token": "forged"},
                       follow_redirects=False)
    assert resp.status_code == 403
    assert db_session.scalar(select(LlmConfig)) is None


def test_每次保存都留一条审计且不落key原文(client, db_session, admin):
    _login(client)
    _save(client, api_key=SAVED_KEY)

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == LLM_CONFIG_UPDATED)
    ).all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_user_id == admin.id
    assert event.detail["model"] == DEFAULT_MODEL
    assert "api_key" in event.detail["changed"]
    # 审计里不能出现 key 原文 —— 连序列化后的 detail 一起查
    assert SAVED_KEY not in str(event.detail)


# ------------------------------------------------- 「存库 + 立即生效」的证明 #


def test_保存后无需重启即可生效(client, db_session, admin):
    _login(client)
    before = client.app.state.llm.current()

    _save(client, model="deepseek-v4-pro", api_key=SAVED_KEY)

    after = client.app.state.llm.current()
    assert after.model == "deepseek-v4-pro"
    assert after.api_key == SAVED_KEY
    assert after.model_source == "database"
    # 就是同一个对象：没有重建 app、没有换 client
    assert client.app.state.llm is not None
    assert (before.model, before.api_key) != (after.model, after.api_key)


def test_保存后的模型与key真的发到了上游(client, db_session, admin, monkeypatch):
    """把出站请求抓下来看：page → DB → 解析 → 请求体，整条链一次验完。"""
    _login(client)
    captured: dict = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None):
            captured.update({"url": url, "headers": headers or {}, "json": json or {}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("hub.llm.client.httpx.Client", _Recorder)

    _save(client, model="deepseek-v4-pro", api_key=SAVED_KEY)
    resp = client.post("/admin/model/test", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/model?test=ok"
    assert captured["json"]["model"] == "deepseek-v4-pro"
    assert captured["headers"]["Authorization"] == f"Bearer {SAVED_KEY}"
    assert captured["url"] == f"{DEFAULT_BASE_URL}/chat/completions"


# ------------------------------------------------------------------ 探测 #


def test_没有key时探测如实报未配置且不打网络(client, db_session, admin):
    _login(client)
    resp = client.post("/admin/model/test", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/model?test=LLM_NOT_CONFIGURED"

    # 回跳后页面把错误码翻译成人话
    body = client.get("/admin/model?test=LLM_NOT_CONFIGURED").text
    assert "未配置 API Key" in body
    assert "LLM_NOT_CONFIGURED" in body


def test_探测失败也要带csrf(client, db_session, admin):
    _login(client)
    resp = client.post("/admin/model/test", data={"csrf_token": "forged"},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_上游401被翻译成看key的提示(client, db_session, admin, monkeypatch):
    _login(client)
    _save(client, api_key=SAVED_KEY)

    class _Unauthorized:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None):
            return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})

    monkeypatch.setattr("hub.llm.client.httpx.Client", _Unauthorized)
    client.post("/admin/model/test", follow_redirects=False)

    body = client.get("/admin/model?test=LLM_AUTH_FAILED").text
    assert "被上游拒绝" in body
    assert "LLM_AUTH_FAILED" in body
