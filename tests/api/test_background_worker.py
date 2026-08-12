import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.background import drive_worker
from hub.db.models import Matter, Output, Round, RoundSummary, Task
from hub.domain.timeutil import utcnow
from hub.main import create_app
from tests.api.test_pipeline_summary import SUMMARY_PAYLOAD
from tests.conftest import make_user


@pytest.fixture()
def collected(db_session):
    """Round 1 awaiting_summary with two submitted outputs (matter in_progress)."""
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
    for t in db_session.scalars(select(Task)).all():
        db_session.execute(
            update(Task).where(Task.id == t.id)
            .values(status="submitted", submitted_at=utcnow())
        )
        db_session.add(
            Output(task_id=t.id,
                   answers=[{"question_id": "q1", "content": "回答"}],
                   notes=None, approved_at=utcnow(), content_digest="x" * 64)
        )
    db_session.execute(
        update(Round).where(Round.id == rnd.id).values(status="awaiting_summary")
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id).values(status="in_progress")
    )
    db_session.commit()
    return {"matter": matter, "round": rnd}


def test_worker_processes_enqueued_round(
    db_session, session_factory, settings, collected, make_fake_llm
):
    llm = make_fake_llm([SUMMARY_PAYLOAD, {"questions": ["追问？"]}])
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(collected["round"].id)

    async def main():
        worker = asyncio.create_task(
            drive_worker(queue, session_factory, settings, llm)
        )
        try:
            await asyncio.wait_for(queue.join(), timeout=10)
        finally:
            worker.cancel()

    asyncio.run(main())
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(RoundSummary)) == 1
    assert db_session.scalar(select(func.count()).select_from(Round)) == 2
    assert db_session.get(Matter, collected["matter"].id).status == "collecting"


def test_worker_survives_pipeline_exception(
    session_factory, settings, collected, make_fake_llm
):
    """管线内部抛非 LLMError 异常时 worker 记录日志并继续，不丢失后续任务。"""

    class ExplodingLLM:
        def complete_json(self, *args, **kwargs):
            raise RuntimeError("unexpected boom")

    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait(collected["round"].id)

    async def main():
        worker = asyncio.create_task(
            drive_worker(queue, session_factory, settings, ExplodingLLM())
        )
        try:
            await asyncio.wait_for(queue.join(), timeout=10)
        finally:
            worker.cancel()

    asyncio.run(main())
    # 异常被吞进日志：轮次保持 awaiting_summary，等 reconciler 下次拾起
    with session_factory() as session:
        assert session.get(Round, collected["round"].id).status == "awaiting_summary"
        assert session.scalar(select(func.count()).select_from(RoundSummary)) == 0


def test_app_startup_reconciles_interrupted_round(
    session_factory, settings, collected, make_fake_llm
):
    """TestClient 进入 lifespan 时 reconciler 拾起 awaiting_summary 轮次。"""
    llm = make_fake_llm([SUMMARY_PAYLOAD, {"questions": ["追问？"]}])
    app = create_app(settings, llm=llm)

    def summary_count() -> int:
        # 每次轮询开新会话：WAL 下长事务读不到新提交的快照
        with session_factory() as s:
            return s.scalar(select(func.count()).select_from(RoundSummary))

    with TestClient(app):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if summary_count() == 1:
                break
            time.sleep(0.1)
    assert summary_count() == 1
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Round)) == 2


def test_create_app_default_llm_uses_settings(settings):
    from hub.llm.client import DeepSeekClient

    app = create_app(settings)
    assert isinstance(app.state.llm, DeepSeekClient)
    assert app.state.drive_queue is not None


def test_resume_worker_processes_enqueued_action(
    db_session, session_factory, settings, make_fake_llm
):
    """resume_queue 元素是 (matter_id, action)；worker 调用
    resume_matter_gate 完成已决决议的传播。"""
    from hub.background import resume_worker
    from hub.db.models import Resolution

    init = make_user(db_session, "init_w")
    alice = make_user(db_session, "alice_w")
    bob = make_user(db_session, "bob_w")
    matter = matter_svc.create_matter(
        db_session, initiator=init, title="T", goal="G", background="B",
        participant_ids=[alice.id, bob.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=init)
    rnd = db_session.scalar(select(Round))
    db_session.execute(
        update(Round).where(Round.id == rnd.id).values(status="closed")
    )
    db_session.add(
        Resolution(
            matter_id=matter.id, source_round_id=rnd.id, version=1,
            status="approved", recommendation="R", rationale="J",
            risks=[], divergences=[], cited_rounds=[1], final_text="R",
        )
    )
    db_session.execute(
        update(Matter).where(Matter.id == matter.id)
        .values(status="awaiting_decision")
    )
    db_session.commit()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait((matter.id, "decide"))

    async def main():
        worker = asyncio.create_task(
            resume_worker(queue, session_factory, settings, make_fake_llm())
        )
        try:
            await asyncio.wait_for(queue.join(), timeout=10)
        finally:
            worker.cancel()

    asyncio.run(main())
    with session_factory() as s:
        assert s.get(Matter, matter.id).status == "completed"
