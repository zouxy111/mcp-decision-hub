# tests/web/test_first_round_web.py
import time

import pytest
from sqlalchemy import func, select

from hub.api import matters as matter_svc
from hub.db.models import Matter, Task
from tests.conftest import make_user


@pytest.fixture()
def app_llm(make_fake_llm):
    return make_fake_llm([{"questions": ["LLM 问题一？", "LLM 问题二？"]}])


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    return init, alice, bob


def _create_blank(db_session, init, alice, bob):
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="选型决策", goal="定下方案",
        background="背景材料",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=[],
    )
    db_session.commit()
    return matter


def test_detail_shows_generating_notice(client, db_session, users):
    """处理中态的确定性断言：直接走服务层开始（不经路由入队），状态停在
    in_progress + generating。"""
    init, alice, bob = users
    matter = _create_blank(db_session, init, alice, bob)
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    _login(client, "init")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "正在生成第一轮问题" in resp.text


def test_web_blank_questions_llm_flow(client, db_session, session_factory):
    """表单留空 → 开始 → 后台出题后出现任务并 collecting（端到端走队列）。"""
    make_user(db_session, "init", password="pw-123456")
    alice_u = make_user(db_session, "alice", password="pw-123456")
    bob_u = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    _login(client, "init")
    data = {
        "title": "选型决策", "goal": "定下方案", "background": "背景材料",
        "timeout_hours": "72", "questions_text": "",
        "participant_ids": [str(alice_u.id), str(bob_u.id)],
    }
    resp = client.post("/matters/new", data=data, follow_redirects=False)
    assert resp.status_code == 303
    matter = db_session.scalar(select(Matter))
    assert matter.draft_questions == []
    resp = client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    assert resp.status_code == 303

    def task_count() -> int:
        # 每次轮询开新会话：WAL 下长事务读不到新提交的快照
        with session_factory() as s:
            return s.scalar(select(func.count()).select_from(Task))

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if task_count() == 2:
            break
        time.sleep(0.1)
    assert task_count() == 2
    with session_factory() as s:
        assert s.get(Matter, matter.id).status == "collecting"
    resp = client.get(f"/matters/{matter.id}")
    assert "LLM 问题一？" in resp.text
