"""D1：四个 MCP 工具返回值的 Pydantic 输出契约。

返回值必须能被 hub/schemas/mcp_outputs.py 的模型逐一校验（extra=forbid），
形状漂移在产出时就失败，而不是在消费端静默变形。键名与既有 JSON 输出
逐一对齐：这里同时锁「键集合」与「模型可校验」两件事。
"""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.db.models import Task
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from hub.mcp_server.methods import (
    mcp_get_matter_status,
    mcp_get_task,
    mcp_list_pending_tasks,
    mcp_submit_output,
)
from hub.schemas import mcp_outputs as out
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


def _submit_payload(task_id, **overrides):
    answers = [{"question_id": "q1", "content": "回答一"},
               {"question_id": "q2", "content": "回答二"}]
    payload = {
        "task_id": task_id,
        "answers": answers,
        "notes": "备注",
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, "备注"),
        "idempotency_key": str(uuid.uuid4()),
    }
    payload.update(overrides)
    if "answers" in overrides or "notes" in overrides:
        payload["content_digest"] = compute_content_digest(
            payload["answers"], payload.get("notes")
        )
    return payload


def test_list_pending_tasks_返回值符合契约(db_session, settings, scenario):
    result = mcp_list_pending_tasks(
        db_session, settings, user_id=scenario["alice"].id, limit=20, cursor=None
    )
    assert set(result.keys()) == {"tasks", "next_cursor", "next_poll_after"}
    assert set(result["tasks"][0].keys()) == {
        "task_id", "matter_id", "matter_title", "round_number",
        "deadline_at", "created_at",
    }
    out.PendingTasksOut.model_validate(result)


def test_get_task_返回值符合契约(db_session, settings, scenario):
    result = mcp_get_task(
        db_session, settings, user_id=scenario["alice"].id,
        task_id=scenario["task_a"].id,
    )
    assert set(result.keys()) == {
        "task_id", "status", "matter", "round", "previous_summary",
        "deadline_at", "llm_provider",
    }
    # round 1 无上一轮 ok 摘要 → previous_summary 必须是键存在且为 None，
    # 而不是「无结论时伪造」（PRD 9.2：绝不编值）
    assert result["previous_summary"] is None
    out.TaskDetailOut.model_validate(result)


def test_submit_output_返回值符合契约(db_session, settings, scenario):
    result = mcp_submit_output(
        db_session, settings, user_id=scenario["alice"].id,
        payload=_submit_payload(scenario["task_a"].id),
    )
    assert set(result.keys()) == {
        "task_id", "status", "submitted_at", "output_id",
    }
    out.SubmitOutputOut.model_validate(result)


def test_submit_output_幂等重放路径也过契约(db_session, settings, scenario):
    payload = _submit_payload(scenario["task_a"].id)
    first = mcp_submit_output(
        db_session, settings, user_id=scenario["alice"].id, payload=payload,
    )
    replay = mcp_submit_output(
        db_session, settings, user_id=scenario["alice"].id, payload=payload,
    )
    assert replay == first
    out.SubmitOutputOut.model_validate(replay)


def test_get_matter_status_参与人视角无participant_progress键(
        db_session, settings, scenario):
    result = mcp_get_matter_status(
        db_session, settings, user_id=scenario["alice"].id,
        matter_id=scenario["matter"].id,
    )
    assert "participant_progress" not in result
    out.MatterStatusOut.model_validate(result)


def test_get_matter_status_发起人视角有participant_progress(
        db_session, settings, scenario):
    result = mcp_get_matter_status(
        db_session, settings, user_id=scenario["init"].id,
        matter_id=scenario["matter"].id,
    )
    assert set(result.keys()) == {
        "matter_id", "title", "status", "rounds_total", "recent_rounds",
        "resolution", "participant_progress",
    }
    assert result["participant_progress"] is not None
    assert result["resolution"] is None  # 未出决议，键在值为 null
    out.MatterStatusOut.model_validate(result)


def test_输出模型禁止多余字段():
    with pytest.raises(ValidationError):
        out.PendingTaskItem.model_validate({
            "task_id": "tsk_x", "matter_id": "mat_x", "matter_title": "T",
            "round_number": 1, "deadline_at": None, "created_at": "2026-01-01T00:00:00Z",
            "sneaky_extra": 1,
        })


def test_输出模型拒绝缺失字段():
    with pytest.raises(ValidationError):
        out.SubmitOutputOut.model_validate({
            "task_id": "tsk_x", "status": "submitted", "submitted_at": "2026-01-01T00:00:00Z",
        })
