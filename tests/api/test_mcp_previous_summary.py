import pytest
from sqlalchemy import select, update

from hub.api import matters as matter_svc
from hub.db.models import Round, RoundSummary, Task
from hub.mcp_server.methods import mcp_get_matter_status, mcp_get_task
from tests.conftest import make_user


@pytest.fixture()
def two_round_scenario(db_session):
    """Matter with round 1 closed (ok summary) and round 2 open with tasks."""
    init = make_user(db_session, "init")
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    db_session.commit()
    rnd1 = db_session.scalar(select(Round).where(Round.round_number == 1))
    db_session.execute(update(Round).where(Round.id == rnd1.id).values(status="closed"))
    db_session.add(
        RoundSummary(
            round_id=rnd1.id, matter_id=matter.id,
            consensus_points=["共识一"], divergences=["分歧一"],
            blind_spots=["盲区一"], open_questions=["未决一"],
            convergence="continue", generation_status="ok",
        )
    )
    rnd2 = Round(matter_id=matter.id, round_number=2, status="open",
                 questions=[{"question_id": "q1", "content": "追问？"}])
    db_session.add(rnd2)
    db_session.flush()
    from datetime import timedelta

    from hub.domain.timeutil import utcnow

    task_a2 = Task(round_id=rnd2.id, matter_id=matter.id, assignee_id=alice.id,
                   status="pending", deadline_at=utcnow() + timedelta(hours=1))
    db_session.add(task_a2)
    db_session.flush()
    task_a1 = db_session.scalar(
        select(Task).where(Task.round_id == rnd1.id, Task.assignee_id == alice.id)
    )
    db_session.commit()
    return {"matter": matter, "alice": alice, "rnd1": rnd1, "rnd2": rnd2,
            "task_a1": task_a1, "task_a2": task_a2}


def test_round_one_task_has_no_previous_summary(db_session, settings, two_round_scenario):
    sc = two_round_scenario
    result = mcp_get_task(db_session, settings, user_id=sc["alice"].id,
                          task_id=sc["task_a1"].id)
    assert result["previous_summary"] is None


def test_round_two_task_gets_real_previous_summary(db_session, settings, two_round_scenario):
    sc = two_round_scenario
    result = mcp_get_task(db_session, settings, user_id=sc["alice"].id,
                          task_id=sc["task_a2"].id)
    prev = result["previous_summary"]
    assert prev is not None
    assert prev["round_number"] == 1
    assert prev["consensus_points"] == ["共识一"]
    assert prev["divergences"] == ["分歧一"]
    assert prev["blind_spots"] == ["盲区一"]
    assert prev["open_questions"] == ["未决一"]
    assert prev["convergence"] == "continue"


def test_failed_previous_summary_is_not_exposed(db_session, settings, two_round_scenario):
    sc = two_round_scenario
    db_session.execute(
        update(RoundSummary)
        .where(RoundSummary.round_id == sc["rnd1"].id)
        .values(generation_status="failed", convergence=None,
                error_code="LLM_TIMEOUT", retry_count=3,
                consensus_points=[], divergences=[], blind_spots=[], open_questions=[])
    )
    db_session.commit()
    result = mcp_get_task(db_session, settings, user_id=sc["alice"].id,
                          task_id=sc["task_a2"].id)
    assert result["previous_summary"] is None


def test_matter_status_summaries_real_data(db_session, settings, two_round_scenario):
    sc = two_round_scenario
    result = mcp_get_matter_status(db_session, settings, user_id=sc["alice"].id,
                                   matter_id=sc["matter"].id)
    assert result["rounds_total"] == 2
    by_number = {r["round_number"]: r for r in result["recent_rounds"]}
    summaries = by_number[1]["summaries"]
    assert len(summaries) == 1
    assert summaries[0]["consensus_points"] == ["共识一"]
    assert summaries[0]["convergence"] == "continue"
    assert summaries[0]["created_at"].endswith("Z")
    assert by_number[2]["summaries"] == []


def test_matter_status_summaries_visible_to_participant(db_session, settings, two_round_scenario):
    # 参与人（非发起人）也能看到摘要（FR-07）；上一条用例已用参与人 alice 验证。
    sc = two_round_scenario
    result = mcp_get_matter_status(db_session, settings, user_id=sc["alice"].id,
                                   matter_id=sc["matter"].id)
    assert any(r["summaries"] for r in result["recent_rounds"])
