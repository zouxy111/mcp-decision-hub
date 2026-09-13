"""rsEXuh：analysis 端点响应必须经 Pydantic 模型（禁止裸 dict）+ faithfulness。

faithfulness 的可测判定标准：
  对外消息中凡承载事实的 sections 字段，必须**逐字等于**事实基座的对应
  字段（决定事项=item_title、要解决的问题=question、结论=decision、
  为什么这么定=rationale、你当初的意见=dissenting[viewer]）；唯一允许的
  非逐字内容是固定的占位文案白名单（无结论/无记录时的固定句式）。
  结构性推论（无需源事实即可校验，落在 StanceAnalysisRead 的校验器上）：
  「结论」为无结论占位文案时，不得同时出现「为什么这么定」/
  「为什么这次没有采纳」—— 没下结论却解释采纳理由，即不忠实。
"""

import pytest

from hub.api.tokens import issue_token
from hub.db.models import Matter, Stance
from hub.schemas.stance import StanceAnalysisRead
from tests.conftest import make_user

NO_DECISION_PLACEHOLDER = "这事目前还没定下来。"


def _bearer(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _stance_row(*, matter_id, user_id, stance, position_summary="p") -> Stance:
    return Stance(
        matter_id=matter_id, user_id=user_id, round_number=1, stance=stance,
        confidence=0.6, position_summary=position_summary,
        rationale_summary="r", non_negotiables=[], conditions=[],
        open_questions=[], depends_on=[], questions_for=[],
        disagreement_kind=None, supersedes=None, acting_as="human",
        authority=None, ttl_seconds=None, urgency="normal",
        visibility="participants", content_hash="c" * 64,
    )


def _setup(db_session, *, position_summary="反对：预算超支不可接受"):
    alice = make_user(db_session, "alice")
    matter = Matter(initiator_id=alice.id, title="是否上线新结算系统",
                    goal="本季度要不要切换结算系统", background="B",
                    initiator_participates=True)
    db_session.add(matter)
    db_session.flush()
    db_session.add(
        _stance_row(matter_id=matter.id, user_id=alice.id, stance="oppose",
                    position_summary=position_summary)
    )
    _t, plaintext = issue_token(db_session, user=alice, name="alice")
    db_session.commit()
    return matter, plaintext


def test_分析端点响应由Pydantic模型承载(client):
    """禁止裸 dict：端点必须声明 response_model，OpenAPI 契约里可见。"""
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/items/{matter_id}/stances/analysis"]["get"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert ref["$ref"].endswith("/StanceAnalysisRead")
    assert "StanceAnalysisRead" in schema["components"]["schemas"]


def test_分析响应逐字忠实于事实基座(client, db_session):
    matter, plaintext = _setup(db_session)

    resp = client.get(f"/api/items/{matter.id}/stances/analysis",
                      headers=_bearer(plaintext))
    assert resp.status_code == 200
    sections = resp.json()["sections"]

    # 承载事实的字段必须逐字等于源事实，不允许改写/意译
    assert sections["决定事项"] == matter.title
    assert sections["要解决的问题"] == matter.goal
    # decision 为空时只允许固定占位文案，不得凭空编结论
    assert sections["结论"] == NO_DECISION_PLACEHOLDER
    # 异议原文逐字转述（dissenting 来自 position_summary）
    assert sections["你当初的意见"] == "反对：预算超支不可接受"


def test_模型拒绝自相矛盾的不忠实载荷():
    """占位结论 + 采纳解释并存 = 不忠实，schema 校验必须拦下。"""
    base = dict(
        fact_version="mat_x:r1",
        violations=[], limitations=["l"], converged=False, degrading=False,
        undetected_checks=[], skipped_user_ids=[],
    )
    with pytest.raises(ValueError):
        StanceAnalysisRead(
            **base,
            sections={
                "结论": NO_DECISION_PLACEHOLDER,
                "为什么这次没有采纳": "这次结论的依据是：x",
            },
        )
