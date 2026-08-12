import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.db.models import Matter, Resolution, Round
from tests.conftest import make_user


@pytest.fixture()
def round1(db_session):
    init = make_user(db_session, "init")
    make_user(db_session, "alice")
    make_user(db_session, "bob")
    matter = Matter(
        initiator_id=init.id, title="T", goal="G", background="B",
        status="in_progress",
    )
    db_session.add(matter)
    db_session.flush()
    rnd = Round(matter_id=matter.id, round_number=1, status="closed",
                questions=[{"question_id": "q1", "content": "Q1?"}])
    db_session.add(rnd)
    db_session.commit()
    return matter, rnd


def _draft(matter, rnd, *, version=1, status="pending_review"):
    return Resolution(
        matter_id=matter.id, source_round_id=rnd.id, version=version,
        status=status,
        recommendation="采用方案 A", rationale="依据", risks=["风险"],
        divergences=["分歧"], cited_rounds=[1],
    )


def test_insert_draft_defaults(round1, db_session):
    matter, rnd = round1
    res = _draft(matter, rnd)
    db_session.add(res)
    db_session.commit()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.id.startswith("res_")
    assert loaded.status == "pending_review"
    assert loaded.version == 1
    assert loaded.final_text is None
    assert loaded.decision_rationale is None
    assert loaded.decided_by is None
    assert loaded.decided_at is None
    assert loaded.created_at is not None
    assert loaded.cited_rounds == [1]


def test_matter_version_unique(round1, db_session):
    matter, rnd = round1
    rnd2 = Round(matter_id=matter.id, round_number=2, status="closed",
                 questions=[])
    db_session.add(rnd2)
    db_session.flush()
    db_session.add(_draft(matter, rnd, version=1))
    db_session.add(_draft(matter, rnd2, version=1, status="rejected"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_source_round_unique(round1, db_session):
    matter, rnd = round1
    db_session.add(_draft(matter, rnd, version=1))
    db_session.add(_draft(matter, rnd, version=2))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_decision_fields_persist(round1, db_session):
    from hub.domain.timeutil import utcnow

    matter, rnd = round1
    res = _draft(matter, rnd)
    db_session.add(res)
    db_session.flush()
    res.status = "modified"
    res.final_text = "最终文本"
    res.decision_rationale = "修改理由"
    res.decided_by = matter.initiator_id
    res.decided_at = utcnow()
    res.version = 2
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.scalar(select(Resolution))
    assert loaded.status == "modified"
    assert loaded.final_text == "最终文本"
    assert loaded.decision_rationale == "修改理由"
    assert loaded.decided_by == matter.initiator_id
    assert loaded.decided_at is not None
    assert loaded.version == 2
