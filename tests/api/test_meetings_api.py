"""会议模式 JSON 接口测试（列表 + 详情）。

既有 3 个会议端点（创建/提交立场/看摘要）此前**没有任何测试覆盖**，
本文件顺带把创建与提交的权限分支也补上，避免这块功能裸奔。

覆盖重点：
- 列表只返回当前用户可见的会议（发起人 + 参与人）
- 详情一次性返回立场、参与者、提交状态、等待名单
- 越权访问返回403/404
"""

import pytest

from tests.conftest import make_user


@pytest.fixture()
def alice(db_session):
    return make_user(db_session, "alice", "pw-12345")


@pytest.fixture()
def bob(db_session):
    return make_user(db_session, "bob", "pw-12345")


@pytest.fixture()
def carol(db_session):
    return make_user(db_session, "carol", "pw-12345")


def _make_matter(db, initiator, title="是否上线新功能", *,
                 background="背景说明", participants=(), **kwargs):
    """建一个事项并挂参与人。返回 Matter。"""
    from hub.db.models import Matter, MatterParticipant

    matter = Matter(
        initiator_id=initiator.id,
        title=title,
        background=background,
        goal="达成一致",
        **kwargs,
    )
    db.add(matter)
    db.flush()
    for user in participants:
        db.add(MatterParticipant(matter_id=matter.id, user_id=user.id))
    db.flush()
    return matter


def _make_meeting(db, matter, *, status="active", **kwargs):
    from hub.db.models import Meeting

    meeting = Meeting(matter_id=matter.id, status=status,
                       timeout_minutes=3, **kwargs)
    db.add(meeting)
    db.flush()
    return meeting


def _login(client, username="alice", password="pw-12345"):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp


# --------------------------------------------------------------------------
# POST /api/meetings（既有端点，顺带补测试）
# --------------------------------------------------------------------------


def test_创建会议_发起人成功(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    _login(client)

    resp = client.post("/api/meetings", json={"matter_id": matter.id,
                                             "timeout_minutes": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert body["matter_id"] == matter.id
    assert body["status"] == "active"
    assert body["timeout_minutes"] == 5
    assert body["round_number"] == 1


def test_创建会议_非发起人被拒403(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    _login(client, "bob")

    resp = client.post("/api/meetings", json={"matter_id": matter.id})

    assert resp.status_code == 403


def test_创建会议_事项不存在返回404(client, db_session, alice):
    _login(client)

    resp = client.post("/api/meetings", json={"matter_id": "mat-不存在"})

    assert resp.status_code == 404


def test_创建会议_未登录重定向(client, db_session, alice):
    matter = _make_matter(db_session, alice)

    resp = client.post("/api/meetings", json={"matter_id": matter.id},
                       follow_redirects=False)

    assert resp.status_code == 303


# --------------------------------------------------------------------------
# GET /api/meetings（新增：列表）
# --------------------------------------------------------------------------


def test_列表_发起人能看到自己事项的会议(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    _make_meeting(db_session, matter)
    _login(client)

    resp = client.get("/api/meetings")

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["matter_title"] == matter.title


def test_列表_参与人也能看到(client, db_session, alice, bob):
    """bob 是参与人不是发起人，应该能看到该会议。"""
    matter = _make_matter(db_session, alice, participants=[bob])
    _make_meeting(db_session, matter)
    _login(client, "bob")

    resp = client.get("/api/meetings")

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_列表_无关人看不到任何会议(client, db_session, alice, bob, carol):
    """carol 既不是发起人也不是参与人 —— 越权看不到。"""
    matter = _make_matter(db_session, alice, participants=[bob])
    _make_meeting(db_session, matter)
    _login(client, "carol")

    resp = client.get("/api/meetings")

    assert resp.status_code == 200
    assert resp.json() == []


def test_列表_按matter_id过滤(client, db_session, alice, bob):
    m1 = _make_matter(db_session, alice, title="议题一", participants=[bob])
    m2 = _make_matter(db_session, alice, title="议题二", participants=[bob])
    _make_meeting(db_session, m1)
    _make_meeting(db_session, m2)
    _login(client)

    resp = client.get(f"/api/meetings?matter_id={m1.id}")

    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["matter_id"] == m1.id


def test_列表_带matter_id但无权限返回403(client, db_session, alice, bob, carol):
    """显式指定他人事项时必须校验权限，不能靠过滤绕过。"""
    matter = _make_matter(db_session, alice, participants=[bob])
    _make_meeting(db_session, matter)
    _login(client, "carol")

    resp = client.get(f"/api/meetings?matter_id={matter.id}")

    assert resp.status_code == 403


def test_列表_含提交进度与应提交人数(client, db_session, alice, bob):
    from hub.db.models import MeetingStance

    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    db_session.add(MeetingStance(id="mst-1", meeting_id=meeting.id,
                                 user_id=bob.id, round_number=1,
                                 text="我同意"))
    db_session.flush()
    _login(client)

    row = client.get("/api/meetings").json()[0]

    assert row["submitted_count"] == 1
    assert row["expected_count"] == 1


def test_列表_发起人参与作答时计入应提交人数(client, db_session, alice):
    """initiator_participates=True 时应提交人数 =参与人+1。"""
    matter = _make_matter(db_session, alice, initiator_participates=True)
    _make_meeting(db_session, matter)
    _login(client)

    row = client.get("/api/meetings").json()[0]

    assert row["expected_count"] == 1


def test_列表_留言板形态发起人不被双算(client, db_session, alice, bob, carol):
    """留言板形态（2026-10-04 起）：发起人**同时**在 matter_participants 里、
    initiator_participates=True。旧口径「行数 +1」会把发起人算两次，
    expected=4 超过实际人数，全员提交也永远凑不齐、只能等超时。
    """
    matter = _make_matter(db_session, alice, participants=[alice, bob, carol],
                          initiator_participates=True)
    _make_meeting(db_session, matter)
    _login(client)

    row = client.get("/api/meetings").json()[0]

    assert row["expected_count"] == 3


def test_收敛判定_留言板形态全员提交即收敛(db_session, alice, bob, carol,
                                         make_fake_llm):
    """_check_and_converge 与 _expected_count 同一口径：全员提交立即可收敛，
    不必等超时。顺带钉住收敛走 complete_json（RuntimeLlm 唯一接口）。"""
    import asyncio

    from hub.api.meetings import _check_and_converge
    from hub.db.models import MeetingStance

    matter = _make_matter(db_session, alice, participants=[alice, bob, carol],
                          initiator_participates=True)
    meeting = _make_meeting(db_session, matter)
    for u in (alice, bob, carol):
        db_session.add(MeetingStance(id=f"mst-{u.id}", meeting_id=meeting.id,
                                     user_id=u.id,
                                     round_number=meeting.round_number,
                                     text="我同意"))
    db_session.flush()

    llm = make_fake_llm([
        {"consensus": ["都同意"], "divergences": [], "follow_ups": []},
    ])
    convergence = asyncio.run(_check_and_converge(db_session, meeting, llm))

    assert convergence is not None
    assert convergence.consensus == ["都同意"]
    assert llm.calls[0]["schema_name"] == "meeting_convergence"


# --------------------------------------------------------------------------
# GET /api/meetings/{id}（新增：详情）
# --------------------------------------------------------------------------


def test_详情_返回主题立场与参与者(client, db_session, alice, bob):
    from hub.db.models import MeetingStance

    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    db_session.add(MeetingStance(id="mst-1", meeting_id=meeting.id,
                                 user_id=bob.id, round_number=1,
                                 text="我同意上线"))
    db_session.flush()
    _login(client, "bob")

    resp = client.get(f"/api/meetings/{meeting.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["matter_title"] == matter.title
    assert body["round_number"] == 1
    assert len(body["stances"]) == 1
    assert body["stances"][0]["text"] == "我同意上线"
    assert body["stances"][0]["username"] == "bob"
    # alice（发起人，默认不参与作答）不该出现在参与者名单里
    assert [p["username"] for p in body["participants"]] == ["bob"]
    assert body["participants"][0]["submitted"] is True
    assert body["self_submitted"] is True
    assert body["waiting_for"] == []


def test_详情_发起人未参与作答时不算进等待名单(client, db_session, alice, bob):
    """默认 initiator_participates=False，发起人不提交立场。

    所以 bob 提交后等待名单应为空 —— 收敛条件按「参与人」判定。
    """
    from hub.db.models import MeetingStance

    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    db_session.add(MeetingStance(id="mst-1", meeting_id=meeting.id,
                                 user_id=bob.id, round_number=1,
                                 text="我同意"))
    db_session.flush()
    _login(client, "alice")  # 发起人视角，本人未提交

    body = client.get(f"/api/meetings/{meeting.id}").json()

    assert body["self_submitted"] is False
    # alice 是发起人且不参与作答，不出现在参与者名单与等待名单里
    assert body["waiting_for"] == []
    assert [p["username"] for p in body["participants"]] == ["bob"]


def test_详情_发起人参与作答时计入等待名单(client, db_session, alice, bob):
    """initiator_participates=True 时，发起人也应收立场。"""
    matter = _make_matter(db_session, alice, participants=[bob],
                          initiator_participates=True)
    meeting = _make_meeting(db_session, matter)
    _login(client, "alice")

    body = client.get(f"/api/meetings/{meeting.id}").json()

    names = [p["username"] for p in body["participants"]]
    assert set(names) == {"alice", "bob"}
    assert body["self_submitted"] is False
    assert set(body["waiting_for"]) == {"alice", "bob"}


def test_详情_参与人视角_别人没提交时自己在等待名单(client, db_session, alice, bob, carol):
    matter = _make_matter(db_session, alice, participants=[bob, carol])
    meeting = _make_meeting(db_session, matter)
    _login(client, "bob")

    body = client.get(f"/api/meetings/{meeting.id}").json()

    assert body["self_submitted"] is False
    assert body["waiting_for"] == ["bob", "carol"]


def test_详情_只返回当前轮立场(client, db_session, alice, bob):
    """上一轮的立场不该混进当前轮视图。"""
    from hub.db.models import MeetingStance

    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter, round_number=2)
    db_session.add(MeetingStance(id="mst-old", meeting_id=meeting.id,
                                 user_id=bob.id, round_number=1,
                                 text="上一轮的话"))
    db_session.add(MeetingStance(id="mst-new", meeting_id=meeting.id,
                                 user_id=bob.id, round_number=2,
                                 text="这一轮的话"))
    db_session.flush()
    _login(client, "bob")

    body = client.get(f"/api/meetings/{meeting.id}").json()

    assert len(body["stances"]) == 1
    assert body["stances"][0]["text"] == "这一轮的话"


def test_详情_无立场时参与者都未提交(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "bob")

    body = client.get(f"/api/meetings/{meeting.id}").json()

    assert body["stances"] == []
    assert body["waiting_for"] == ["bob"]
    assert body["self_submitted"] is False


def test_详情_非成员返回403(client, db_session, alice, bob, carol):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "carol")

    resp = client.get(f"/api/meetings/{meeting.id}")

    assert resp.status_code == 403


def test_详情_会议不存在返回404(client, db_session, alice):
    _login(client)

    resp = client.get("/api/meetings/mtg-不存在")

    assert resp.status_code == 404


def test_详情_未登录重定向(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)

    resp = client.get(f"/api/meetings/{meeting.id}", follow_redirects=False)

    assert resp.status_code == 303


# --------------------------------------------------------------------------
# 提交立场（既有端点，补关键分支）
# --------------------------------------------------------------------------


def test_提交立场_记录并返回等待名单(client, db_session, alice, bob, carol):
    matter = _make_matter(db_session, alice, participants=[bob, carol])
    meeting = _make_meeting(db_session, matter)
    _login(client, "bob")

    resp = client.post(f"/api/meetings/{meeting.id}/stances",
                       json={"text": "我同意", "round": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert body["converged"] is False
    assert body["waiting_for"] == ["carol"]


def test_提交立场_同轮重复提交返回409(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "bob")
    client.post(f"/api/meetings/{meeting.id}/stances",
                json={"text": "第一遍", "round": 1})

    resp = client.post(f"/api/meetings/{meeting.id}/stances",
                       json={"text": "再说一次", "round": 1})

    assert resp.status_code == 409


def test_提交立场_非成员返回403(client, db_session, alice, bob, carol):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "carol")

    resp = client.post(f"/api/meetings/{meeting.id}/stances",
                       json={"text": "我来插一句", "round": 1})

    assert resp.status_code == 403


def test_提交立场_会议已结束返回400(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter, status="completed")
    _login(client, "bob")

    resp = client.post(f"/api/meetings/{meeting.id}/stances",
                       json={"text": "晚了", "round": 1})

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# GET /api/meetings/{id}/summary（既有端点）
# --------------------------------------------------------------------------


def test_摘要_未收敛时ready为false(client, db_session, alice, bob):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "bob")

    resp = client.get(f"/api/meetings/{meeting.id}/summary")

    assert resp.status_code == 200
    assert resp.json()["ready"] is False


def test_摘要_有收敛结果时返回共识分歧追问(client, db_session, alice, bob):
    from hub.db.models import MeetingConvergence

    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter, round_number=2)
    db_session.add(MeetingConvergence(
        id="mcv-1", meeting_id=meeting.id, round_number=1,
        consensus=["都同意先小范围试"], divergences=["推广范围有分歧"],
        follow_ups=["谁负责灰度名单？"]))
    db_session.flush()
    _login(client, "bob")

    body = client.get(f"/api/meetings/{meeting.id}/summary").json()

    assert body["ready"] is True
    assert body["round"] == 1
    assert body["summary"]["consensus"] == ["都同意先小范围试"]
    assert body["summary"]["follow_ups"] == ["谁负责灰度名单？"]


def test_摘要_非成员返回403(client, db_session, alice, bob, carol):
    matter = _make_matter(db_session, alice, participants=[bob])
    meeting = _make_meeting(db_session, matter)
    _login(client, "carol")

    resp = client.get(f"/api/meetings/{meeting.id}/summary")

    assert resp.status_code == 403