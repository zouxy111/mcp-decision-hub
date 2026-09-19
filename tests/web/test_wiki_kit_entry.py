"""wiki-knowledgebase-share-kit 顶栏入口测试（2026-09-20 新增）。

来由：用户要求「首页顶栏 Skills 后面加一栏 wiki-knowledgebase-share-kit，
点开是项目介绍和拉取命令」。介绍页与一键安装命令住在独立产品站
wiki.tdp-demo.work（与 hub 同机、Caddy 反代），所以入口是外链，不是应用内路由。
"""

import pytest

from tests.conftest import make_user

WIKI_URL = "https://wiki.tdp-demo.work/"
WIKI_LABEL = "wiki-knowledgebase-share-kit"


@pytest.fixture()
def member(db_session):
    """非管理员普通成员 —— 入口对所有人开放，不该只有 admin 看得到。"""
    u = make_user(db_session, "member", password="pw-123456")
    db_session.commit()
    return u


def _login(client, username="member", password="pw-123456"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def _topbar(client):
    return client.get("/dashboard").text


def test_登录后顶栏有wiki入口且指向产品站(client, member):
    _login(client)
    body = _topbar(client)

    assert f'href="{WIKI_URL}"' in body
    assert WIKI_LABEL in body


def test_入口位于Skills之后(client, member):
    """用户点名「放在 skills 的后面一栏」，顺序是需求本身。"""
    _login(client)
    body = _topbar(client)

    assert body.index('href="/static/skills/"') < body.index(f'href="{WIKI_URL}"')


def test_外链必须新开页且不带opener(client, member):
    """独立产品站：新开页别打断 hub 会话；noopener 防新页反向控制本页。"""
    _login(client)
    body = _topbar(client)

    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body


def test_未登录时不渲染该入口(client):
    """与主导航整体一致：登录页没有任何可达业务页，不渲染导航。"""
    body = client.get("/login").text

    assert f'href="{WIKI_URL}"' not in body
