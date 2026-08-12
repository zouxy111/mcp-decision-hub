import pytest
from sqlalchemy import select

from hub.api import audit as audit_mod
from hub.api import matters as matter_svc
from hub.api.audit_query import query_audit_events
from hub.db.models import AuditEvent
from hub.domain.timeutil import utcnow
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    admin = make_user(db_session, "admin_u", password="pw-123456", is_admin=True)
    init = make_user(db_session, "init", password="pw-123456")
    alice = make_user(db_session, "alice", password="pw-123456")
    bob = make_user(db_session, "bob", password="pw-123456")
    db_session.commit()
    return {"admin": admin, "init": init, "alice": alice, "bob": bob}


@pytest.fixture()
def scenario(db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="选型", goal="定方案",
        background="背景",
        participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    db_session.commit()
    return matter


def _login(client, username):
    client.post("/login", data={"username": username, "password": "pw-123456"},
                follow_redirects=False)


def test_query_filters_by_matter_and_event_type(db_session, scenario):
    rows, has_more = query_audit_events(db_session, matter_id=scenario.id)
    assert [r.event_type for r in rows] == ["matter_created"]
    assert has_more is False
    rows, _ = query_audit_events(
        db_session, matter_id=scenario.id, event_type="resolution_decided"
    )
    assert rows == []


def test_query_filters_by_actor_and_time(db_session, users, scenario):
    from datetime import timedelta

    rows, _ = query_audit_events(db_session, actor_user_id=users["init"].id)
    assert len(rows) == 1
    rows, _ = query_audit_events(db_session, actor_user_id=users["alice"].id)
    assert rows == []
    rows, _ = query_audit_events(db_session, since=utcnow() + timedelta(days=1))
    assert rows == []
    rows, _ = query_audit_events(db_session, until=utcnow() - timedelta(days=1))
    assert rows == []


def test_query_pagination(db_session, users, scenario):
    for _ in range(5):
        audit_mod.record_audit(db_session, audit_mod.MATTER_CONTINUED,
                               matter_id=scenario.id,
                               detail={"granted_extra_rounds": 0})
    db_session.commit()
    rows, has_more = query_audit_events(db_session, matter_id=scenario.id,
                                        limit=3, offset=0)
    assert len(rows) == 3
    assert has_more is True
    rows2, has_more2 = query_audit_events(db_session, matter_id=scenario.id,
                                          limit=3, offset=3)
    assert len(rows2) == 3
    assert has_more2 is False
    assert {r.id for r in rows}.isdisjoint({r.id for r in rows2})


def test_admin_audit_page_filters(client, scenario):
    _login(client, "admin_u")
    resp = client.get("/admin/audit",
                      params={"matter_id": scenario.id,
                              "event_type": "matter_created"})
    assert resp.status_code == 200
    assert "matter_created" in resp.text
    assert "init" in resp.text  # 操作者用户名
    resp = client.get("/admin/audit", params={"event_type": "no_such_event"})
    assert resp.status_code == 200
    assert "无匹配审计记录" in resp.text


def test_admin_audit_page_forbidden_for_non_admin(client, users):
    _login(client, "init")
    resp = client.get("/admin/audit")
    assert resp.status_code == 403


def test_admin_audit_is_readonly(client, users):
    _login(client, "admin_u")
    resp = client.post("/admin/audit", data={})
    assert resp.status_code == 405  # 只读：无写路由


def test_matter_audit_page_initiator_ok(client, scenario):
    _login(client, "init")
    resp = client.get(f"/matters/{scenario.id}/audit")
    assert resp.status_code == 200
    assert "matter_created" in resp.text


def test_matter_audit_page_participant_403(client, db_session, scenario):
    _login(client, "alice")
    resp = client.get(f"/matters/{scenario.id}/audit")
    assert resp.status_code == 403
    db_session.expire_all()
    events = [
        r for r in db_session.scalars(select(AuditEvent)).all()
        if r.event_type == "forbidden_denied"
        and (r.detail or {}).get("action") == "view_matter_audit"
    ]
    assert len(events) == 1


def test_matter_audit_page_outsider_404(client, db_session, users, scenario):
    make_user(db_session, "outsider", password="pw-123456")
    db_session.commit()
    _login(client, "outsider")
    resp = client.get(f"/matters/{scenario.id}/audit")
    assert resp.status_code == 404


def test_detail_audit_block_initiator_only(client, scenario):
    _login(client, "init")
    resp = client.get(f"/matters/{scenario.id}")
    assert "本事项审计" in resp.text
    assert "matter_created" in resp.text
    client2 = client
    client2.cookies.clear()
    _login(client2, "alice")
    resp = client2.get(f"/matters/{scenario.id}")
    assert "本事项审计" not in resp.text
