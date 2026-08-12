import pytest
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.pipeline import run_round_pipeline
from hub.db.models import Matter, Resolution, Round, RoundSummary, Task
from hub.graph.matter_graph import (
    build_matter_graph,
    drive_matter_tick,
    open_checkpointer,
    sqlite_path_from_url,
)
from tests.conftest import make_user

SUMMARY_CONTINUE = {
    "consensus_points": ["共识"], "divergences": ["分歧"],
    "blind_spots": [], "open_questions": ["未决"], "convergence": "continue",
}
SUMMARY_CONVERGED = {**SUMMARY_CONTINUE, "convergence": "converged"}
SUMMARY_PROVISIONAL = {**SUMMARY_CONTINUE,
                       "convergence": "provisionally_ready"}
DRAFT_PAYLOAD = {
    "recommendation": "采用方案 A", "rationale": "依据",
    "risks": ["风险"], "divergences": [], "cited_rounds": [1],
}


def _pending_node(session_factory, settings, matter_id):
    """Build the graph against the same sqlite file and read the pending
    node for the matter's thread (test introspection helper)."""
    path = sqlite_path_from_url(settings.database_url)
    saver = open_checkpointer(path)
    try:
        graph = build_matter_graph(
            session_factory=session_factory, settings=settings,
            llm=None, checkpointer=saver,
        )
        snapshot = graph.get_state({"configurable": {"thread_id": matter_id}})
        interrupts = [i for t in snapshot.tasks for i in t.interrupts]
        return snapshot.next, interrupts
    finally:
        saver.conn.close()


@pytest.fixture()
def scenario(db_session):
    """Matter with round 1 open (manual questions), two participants."""
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


def _close_round_with_summary(db_session, scenario, convergence):
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id).values(status="closed")
    )
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="in_progress")
    )
    db_session.add(
        RoundSummary(
            round_id=scenario["round"].id, matter_id=scenario["matter"].id,
            consensus_points=["共识"], divergences=["分歧"],
            blind_spots=[], open_questions=["未决"],
            convergence=convergence, generation_status="ok",
        )
    )
    db_session.commit()


def test_sqlite_path_from_url():
    assert sqlite_path_from_url("sqlite:////tmp/x.db") == "/tmp/x.db"
    assert sqlite_path_from_url("sqlite:///relative.db") == "relative.db"
    with pytest.raises(ValueError):
        sqlite_path_from_url("sqlite:///:memory:")
    with pytest.raises(ValueError):
        sqlite_path_from_url("postgresql://localhost/x")


def test_tick_generates_first_round_questions(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """LLM 路径首轮：空 generating 轮次经 tick 出题（与 M2 首轮相位等价）。"""
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="generating", questions=[])
    )
    db_session.execute(
        update(Task)  # 手动路径已建任务，清掉模拟 LLM 路径
        .where(Task.round_id == scenario["round"].id).values(status="cancelled")
    )
    db_session.commit()
    # 删除手动任务，模拟 LLM 路径（generating 空轮次 + 无任务）
    for t in db_session.scalars(select(Task)).all():
        db_session.delete(t)
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="in_progress")
    )
    db_session.commit()
    llm = make_fake_llm([{"questions": ["LLM 题一？", "LLM 题二？"]}])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    db_session.expire_all()
    matter = db_session.get(Matter, scenario["matter"].id)
    assert matter.status == "collecting"
    rnd = db_session.get(Round, scenario["round"].id)
    assert rnd.status == "open"
    assert [q["content"] for q in rnd.questions] == ["LLM 题一？", "LLM 题二？"]
    assert db_session.scalar(select(func.count()).select_from(Task)) == 2


def test_tick_summarizes_and_continues(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """收齐轮次经 tick 走 summarize→branch→追问开新轮（M2 行为不变）。"""
    db_session.execute(
        update(Round).where(Round.id == scenario["round"].id)
        .values(status="awaiting_summary")
    )
    db_session.execute(
        update(Matter)
        .where(Matter.id == scenario["matter"].id).values(status="in_progress")
    )
    db_session.execute(update(Task).values(status="submitted"))
    from hub.db.models import Output
    from hub.domain.timeutil import utcnow
    for t in db_session.scalars(select(Task)).all():
        db_session.add(
            Output(task_id=t.id,
                   answers=[{"question_id": "q1", "content": "回答"}],
                   notes=None, approved_at=utcnow(), content_digest="x" * 64)
        )
    db_session.commit()
    llm = make_fake_llm([SUMMARY_CONTINUE, {"questions": ["追问？"]}])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    assert len(llm.calls) == 2
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "collecting"
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2


def test_tick_converged_drafts_and_stops_at_end(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """本任务图尚无闸门节点：converged → 草案 + awaiting_decision，图到
    END（不挂起）。任务 10 会把这里演进为挂 decision_gate。"""
    _close_round_with_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1
    next_nodes, interrupts = _pending_node(
        session_factory, settings, scenario["matter"].id
    )
    assert next_nodes == ()
    assert interrupts == []


def test_tick_provisional_drafts_and_stays_in_progress(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _close_round_with_summary(db_session, scenario, "provisionally_ready")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "in_progress"
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1


def test_redrive_tick_is_idempotent(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _close_round_with_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    assert len(llm.calls) == 1
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1


def test_tick_continues_after_midrun_crash(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    """崩溃在节点之间（pending 节点、无 interrupt）：tick 用 invoke(None)
    续跑而不是从 START 重跑（验证结论 8）。"""
    _close_round_with_summary(db_session, scenario, "converged")
    # 模拟崩溃：checkpoint 停在 summarize 之后、branch 之前
    path = sqlite_path_from_url(settings.database_url)
    saver = open_checkpointer(path)
    try:
        graph = build_matter_graph(
            session_factory=session_factory, settings=settings,
            llm=None, checkpointer=saver,
        )
        config = {"configurable": {"thread_id": scenario["matter"].id}}
        graph.update_state(
            config, {"matter_id": scenario["matter"].id}, as_node="summarize"
        )
        assert graph.get_state(config).next == ("branch",)
    finally:
        saver.conn.close()
    llm = make_fake_llm([DRAFT_PAYLOAD])
    drive_matter_tick(session_factory, settings,
                      matter_id=scenario["matter"].id, llm=llm)
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"


def test_run_round_pipeline_delegates_to_tick(
    db_session, session_factory, settings, scenario, make_fake_llm
):
    _close_round_with_summary(db_session, scenario, "converged")
    llm = make_fake_llm([DRAFT_PAYLOAD])
    run_round_pipeline(session_factory, settings,
                       round_id=scenario["round"].id, llm=llm)
    db_session.expire_all()
    assert db_session.get(Matter, scenario["matter"].id).status == "awaiting_decision"
    assert db_session.scalar(select(func.count()).select_from(Resolution)) == 1


def test_run_round_pipeline_unknown_round_is_noop(
    session_factory, settings, make_fake_llm
):
    llm = make_fake_llm()
    run_round_pipeline(session_factory, settings, round_id="rnd_nope", llm=llm)
    assert len(llm.calls) == 0
