import pytest
from sqlalchemy import select

from hub.db.models import Matter, Round, Task
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    init = make_user(db_session, "init", password="pw-123456")
    a = make_user(db_session, "alice", password="pw-123456")
    b = make_user(db_session, "bob", password="pw-123456")
    outsider = make_user(db_session, "outsider", password="pw-123456")
    db_session.commit()
    return init, a, b, outsider


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def _create_form(a, b, **overrides):
    data = {
        "title": "选型决策", "goal": "定下方案", "background": "背景材料",
        "timeout_hours": "72", "questions_text": "问题一？\n问题二？",
        "participant_ids": [str(a.id), str(b.id)],
    }
    data.update(overrides)
    return data


def test_new_page_shows_notice_and_users(client, users, settings):
    init, a, b, _ = users
    _login(client, "init")
    resp = client.get("/matters/new")
    assert resp.status_code == 200
    assert "第三方大模型服务商" in resp.text
    assert "骨架阶段" not in resp.text
    assert settings.llm_provider_name in resp.text
    assert "alice" in resp.text and "bob" in resp.text


def test_create_with_one_participant_shows_error(client, users):
    init, a, b, _ = users
    _login(client, "init")
    resp = client.post("/matters/new", data=_create_form(a, b,
                                                         participant_ids=[str(a.id)]))
    assert resp.status_code == 200
    assert "2–5" in resp.text


def test_create_success_redirects_to_detail(client, users, db_session):
    init, a, b, _ = users
    _login(client, "init")
    resp = client.post("/matters/new", data=_create_form(a, b),
                       follow_redirects=False)
    assert resp.status_code == 303
    matter = db_session.scalar(select(Matter))
    assert resp.headers["location"] == f"/matters/{matter.id}"
    assert matter.status == "draft"


def test_start_via_web_creates_tasks(client, users, db_session):
    init, a, b, _ = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    matter = db_session.scalar(select(Matter))
    resp = client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.scalar(select(Matter)).status == "collecting"
    tasks = db_session.scalars(select(Task)).all()
    assert len(tasks) == 2
    rnd = db_session.scalar(select(Round))
    assert [q["question_id"] for q in rnd.questions] == ["q1", "q2"]


def test_double_start_shows_409_error(client, users, db_session):
    init, a, b, _ = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    matter = db_session.scalar(select(Matter))
    client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    resp = client.post(f"/matters/{matter.id}/start")
    assert resp.status_code == 409
    assert "不允许开始" in resp.text


def test_detail_outsider_404(client, users, db_session):
    init, a, b, outsider = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    matter = db_session.scalar(select(Matter))
    _login(client, "outsider")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 404


def test_detail_participant_sees_own_task_only(client, users, db_session):
    init, a, b, _ = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    matter = db_session.scalar(select(Matter))
    client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    _login(client, "alice")
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "collecting" in resp.text
    assert "我的任务" in resp.text
    # participant must NOT see other assignees' task table
    assert "全部任务" not in resp.text


def test_detail_initiator_sees_all_tasks_and_provider(client, users, db_session,
                                                      settings):
    init, a, b, _ = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    matter = db_session.scalar(select(Matter))
    client.post(f"/matters/{matter.id}/start", follow_redirects=False)
    resp = client.get(f"/matters/{matter.id}")
    assert resp.status_code == 200
    assert "全部任务" in resp.text
    assert "alice" in resp.text and "bob" in resp.text
    assert settings.llm_provider_name in resp.text


def test_dashboard_lists_only_own_matters(client, users, db_session):
    init, a, b, outsider = users
    _login(client, "init")
    client.post("/matters/new", data=_create_form(a, b), follow_redirects=False)
    _login(client, "outsider")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "选型决策" not in resp.text
    _login(client, "alice")
    resp = client.get("/dashboard")
    assert "选型决策" in resp.text
