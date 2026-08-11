import json

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import Task
from hub.mcp_server.methods import mcp_get_task, mcp_list_pending_tasks
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?", "Q2?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    task_a = db_session.scalar(select(Task).where(Task.assignee_id == alice.id))
    task_b = db_session.scalar(select(Task).where(Task.assignee_id == bob.id))
    return {"init": init, "alice": alice, "bob": bob, "matter": matter,
            "task_a": task_a, "task_b": task_b}


def test_list_returns_only_own_pending(db_session, settings, scenario):
    result = mcp_list_pending_tasks(
        db_session, settings, user_id=scenario["alice"].id, limit=20, cursor=None
    )
    assert [t["task_id"] for t in result["tasks"]] == [scenario["task_a"].id]
    assert result["tasks"][0]["matter_title"] == "T"
    assert result["tasks"][0]["round_number"] == 1
    assert result["next_cursor"] is None
    # has pending -> 30s poll hint
    assert "next_poll_after" in result


def test_list_empty_returns_300s_poll_hint(db_session, settings, scenario):
    result = mcp_list_pending_tasks(
        db_session, settings, user_id=scenario["init"].id, limit=20, cursor=None
    )
    assert result["tasks"] == []
    assert result["next_cursor"] is None
    assert result["next_poll_after"].endswith("Z")


def test_list_cursor_pagination(db_session, settings, scenario):
    # give bob a second pending task via another matter
    init, bob = scenario["init"], scenario["bob"]
    carol = make_user(db_session, "carol")
    m2 = matter_svc.create_matter(
        db_session, initiator=init, title="T2", goal="G", background="B",
        participant_ids=[bob.id, carol.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=m2.id, actor=init)
    db_session.commit()
    page1 = mcp_list_pending_tasks(
        db_session, settings, user_id=bob.id, limit=1, cursor=None
    )
    assert len(page1["tasks"]) == 1
    assert page1["next_cursor"] is not None
    page2 = mcp_list_pending_tasks(
        db_session, settings, user_id=bob.id, limit=1,
        cursor=page1["next_cursor"],
    )
    assert len(page2["tasks"]) == 1
    assert page2["tasks"][0]["task_id"] != page1["tasks"][0]["task_id"]
    assert page2["next_cursor"] is None


def test_list_invalid_cursor_422(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        mcp_list_pending_tasks(
            db_session, settings, user_id=scenario["alice"].id, limit=20,
            cursor="!!!not-base64!!!",
        )
    assert exc.value.error_code == "CURSOR_INVALID"


def test_list_limit_clamped_to_100(db_session, settings, scenario):
    # limit > 100 must not raise; it clamps
    result = mcp_list_pending_tasks(
        db_session, settings, user_id=scenario["alice"].id, limit=500, cursor=None
    )
    assert len(result["tasks"]) == 1


def test_get_task_returns_context_package(db_session, settings, scenario):
    result = mcp_get_task(
        db_session, settings, user_id=scenario["alice"].id,
        task_id=scenario["task_a"].id,
    )
    assert result["matter"]["title"] == "T"
    assert result["matter"]["background"] == "B"
    assert result["matter"]["goal"] == "G"
    assert [q["question_id"] for q in result["round"]["questions"]] == ["q1", "q2"]
    assert result["previous_summary"] is None  # M1: no summaries, never fabricate
    assert result["llm_provider"] == settings.llm_provider_name
    assert result["deadline_at"].endswith("Z")
    # no other participants' raw answers anywhere in the payload
    assert "answers" not in json.dumps(result)


def test_get_task_other_user_403_and_audited(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        mcp_get_task(
            db_session, settings, user_id=scenario["alice"].id,
            task_id=scenario["task_b"].id,
        )
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FORBIDDEN_SCOPE"
    from sqlalchemy import select as sa_select

    from hub.db.models import AuditEvent
    rows = db_session.scalars(
        sa_select(AuditEvent).where(AuditEvent.event_type == "forbidden_denied")
    ).all()
    assert len(rows) == 1


def test_get_task_missing_404(db_session, settings, scenario):
    with pytest.raises(ApiError) as exc:
        mcp_get_task(
            db_session, settings, user_id=scenario["alice"].id,
            task_id="tsk_nope",
        )
    assert exc.value.error_code == "RESOURCE_NOT_FOUND"
