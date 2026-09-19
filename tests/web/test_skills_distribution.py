"""skills 分发点与静态挂载测试（2026-09-19 新增）。

来由：落地页上线后被反馈「skills 下载我在生产机上没看到」。查下来是三个独立原因，
都不是「文件没传上去」——生产机上 7 个文件都在、公网也都能取到：

  1. **产品里没有任何入口**。顶栏 7 项里没有 skills，`grep -rn "skills"
     hub/web/templates/` 与 `hub/web/*.py` 都是零命中。用户没有「看到」它的路径。
  2. **目录地址打不开**。`app.mount("/static", StaticFiles(...))` 没传 `html=True`，
     所以 `/static/skills` 与 `/static/skills/` 都返回 404，只有写全
     `/static/skills/index.html` 才通 —— 而没人会去猜那个文件名。
  3. **落地页 favicon 停在改色前的琥珀** `%23ffa03c`。改「朱印」时我漏了这个文件。

三个都属于同一类问题：**没有任何断言在管分发点**。所以补上，每条断言对应一个
已发生的故障，而不是凭空设的。
"""

import pytest

from tests.conftest import make_user

SKILL_A = "connect-decision-hub"
SKILL_B = "build-decision-model"

# 落地页必须挂出来的分发物。少一个就意味着「页面上点不到」。
ARTIFACTS = [
    "install.sh",
    "skills.tar.gz",
    f"{SKILL_A}.tar.gz",
    f"{SKILL_B}.tar.gz",
    f"{SKILL_A}/SKILL.md",
    f"{SKILL_B}/SKILL.md",
]

PUBLIC_BASE = "https://hub.tdp-demo.work/static/skills"


@pytest.fixture()
def member(db_session):
    """非管理员普通成员 —— 分发点对所有人开放，不该只有 admin 看得到。"""
    u = make_user(db_session, "member", password="pw-123456")
    db_session.commit()
    return u


def _login(client, username="member", password="pw-123456"):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


# ------------------------------------------------------------ 匿名可达（curl 前提） #


def test_匿名就能取到每个分发物(client):
    """分发点必须免登录 —— 一行命令 `curl | tar` 里没有会话，登录墙会直接把它废掉。"""
    for name in ARTIFACTS:
        resp = client.get(f"/static/skills/{name}")
        assert resp.status_code == 200, f"{name} 匿名取不到"
        assert len(resp.content) > 0, f"{name} 内容为空"


def test_目录索引能直接打开落地页(client):
    """故障 2：没有 html=True 时这里返回 404，只能写全 index.html 才通。"""
    resp = client.get("/static/skills/")

    assert resp.status_code == 200
    assert "可拉取的 Skill 包" in resp.text


def test_省略尾斜杠的地址也能到达落地页(client):
    """用户手输地址多半不会带尾斜杠，不能因此吃到 404。"""
    resp = client.get("/static/skills", follow_redirects=True)

    assert resp.status_code == 200
    assert "可拉取的 Skill 包" in resp.text


# ------------------------------------------------------------------ 落地页自身 #


def test_落地页把每个分发物都挂成了链接(client):
    body = client.get("/static/skills/index.html").text

    for name in ARTIFACTS:
        assert name in body, f"落地页没有挂出 {name}"


def test_落地页给出的一行命令与站点真实地址一致(client):
    """命令里写死的域名必须就是分发点自己的地址，否则复制出去就是 404。"""
    body = client.get("/static/skills/index.html").text

    assert f"{PUBLIC_BASE}/skills.tar.gz" in body
    assert f"{PUBLIC_BASE}/install.sh" in body


def test_落地页不该残留改色前的琥珀(client):
    """故障 3：改「朱印」时漏了这个文件的 favicon，旧amber一直挂在页面标签上。"""
    body = client.get("/static/skills/index.html").text

    assert "%23ffa03c" not in body, "落地页还留着旧琥珀 favicon"
    assert "%23e85c28" in body, "落地页没有用现行主信号色"


# ------------------------------------------------------------------ 产品内入口 #


def test_登录后顶栏有Skills入口(client, member):
    """故障 1：顶栏原本对 skills 零引用，用户无从发现。"""
    _login(client)
    resp = client.get("/dashboard")

    assert resp.status_code == 200
    assert 'href="/static/skills/"' in resp.text


def test_普通成员也能看到入口而非仅管理员(client, member):
    """分发是给每个人用的，不该只挂在 admin 区块里。"""
    _login(client)
    body = client.get("/dashboard").text

    assert 'href="/static/skills/"' in body


def test_未登录时不渲染该入口(client):
    """登录页没有任何可达业务页，渲染导航只会让用户点了被弹回。"""
    resp = client.get("/login")

    assert resp.status_code == 200
    assert 'href="/static/skills/"' not in resp.text


# ------------------------------------------------------- 回归：挂载改动不能伤到别的 #


def test_console_css_仍能取到且非空(client):
    """加 html=True 是改挂载参数，不能把既有的静态资源分发一起弄坏。"""
    resp = client.get("/static/console.css")

    assert resp.status_code == 200
    assert len(resp.content) > 0
