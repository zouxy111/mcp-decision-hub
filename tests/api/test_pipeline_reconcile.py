import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import find_interrupted_round_ids, run_round_pipeline
from hub.db.models import Matter, Round, RoundSummary
from tests.api.test_pipeline_summary import SUMMARY_PAYLOAD
from tests.conftest import make_user


@pytest.fixture()
def scenario(db_session):
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
    rnd = db_session.scalar(select(Round))
    return {"matter": matter, "round": rnd}


def test_awaiting_summary_without_summary_row_is_picked_up(db_session, scenario):
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="in_progress")
    )
    db_session.commit()
    assert find_interrupted_round_ids(db_session) == [scenario["round"].id]


def test_awaiting_summary_with_ok_summary_is_skipped(db_session, scenario):
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=["已有"], divergences=[], blind_spots=[],
            open_questions=[], convergence="continue", generation_status="ok",
        )
    )
    db_session.commit()
    assert find_interrupted_round_ids(db_session) == []


def test_failed_summary_exhausted_retries_is_skipped(db_session, scenario):
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=[], divergences=[], blind_spots=[], open_questions=[],
            convergence=None, generation_status="failed",
            error_code="LLM_TIMEOUT", retry_count=3,
        )
    )
    db_session.commit()
    assert find_interrupted_round_ids(db_session) == []


def test_in_progress_matter_without_active_round_is_picked_up(db_session, scenario):
    # 分支阶段中断：事项 in_progress，最新轮已 closed 且无新轮
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id).values(status="closed")
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="in_progress")
    )
    db_session.commit()
    assert find_interrupted_round_ids(db_session) == [scenario["round"].id]


def test_collecting_matter_is_not_interrupted(db_session, scenario):
    assert find_interrupted_round_ids(db_session) == []


def test_generating_round_with_empty_questions_is_picked_up(db_session, scenario):
    # 首轮 LLM 出题中断（任务 13 的路径）：generating 且 questions 为空
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="generating", questions=[])
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="in_progress")
    )
    db_session.commit()
    assert scenario["round"].id in find_interrupted_round_ids(db_session)


def test_generating_round_with_manual_questions_is_skipped(db_session, scenario):
    # 手动路径的 generating 轮次（questions 已填）不由 reconciler 重驱动
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="generating")
    )
    db_session.commit()
    assert find_interrupted_round_ids(db_session) == []


def test_reconcile_then_drive_does_not_duplicate_summary(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    # 完整跑通一次（摘要 + 第二轮），再模拟"awaiting_summary 但已有 ok 摘要"
    # 的中断态：reconciler 不应拾起；即使被驱动也不重复生成。
    from hub.db.models import Output, Task
    from hub.domain.timeutil import utcnow

    for t in db_session.scalars(select(Task)).all():
        db_session.execute(
            update(Task).where(Task.id == t.id).values(status="submitted")
        )
        db_session.add(
            Output(task_id=t.id,
                   answers=[{"question_id": "q1", "content": "回答"}],
                   notes=None, approved_at=utcnow(), content_digest="x" * 64)
        )
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.execute(
        update(Matter).where(Matter.id == scenario["matter"].id)
        .values(status="in_progress")
    )
    db_session.commit()
    llm = make_fake_llm([SUMMARY_PAYLOAD, {"questions": ["追问？"]}])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert db_session.scalar(select(func.count()).select_from(RoundSummary)) == 1

    # 手工把第一轮拨回 awaiting_summary（模拟异常中断态），ok 摘要仍在
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.commit()
    assert scenario["round"].id not in find_interrupted_round_ids(db_session)
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    assert db_session.scalar(select(func.count()).select_from(RoundSummary)) == 1
    assert len(llm.calls) == 2  # 未发生第三次 LLM 调用
