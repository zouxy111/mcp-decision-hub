"""2026-09-14 owner 裁决落地（TDD）。

裁决 5a：irreversible 变更理由必填（创建时勾选 irreversible=True 必须带
irreversible_reason）。
裁决 5b：can_commit 开通限非终态事项（终态后不允许开通）。
裁决 2：decide_item 仅生成决议草稿（不写 completed；irreversible 事项直接
拒绝终裁）+ 授权范围可配置（用户自定义哪些问题可授权 AI 拍板）。
裁决 3：get_digest = 一页纸现状（当前轮次 + 最新结论/决议草案 + 未决开口）。
注：`873ff8d` 落地时简化成「最新 ok 摘要 + 状态 + 收敛」的简单形态，
`decision` 一节缺失；2026-09-17 按 owner 口径补齐（口径自 09-14 起未变，
09-16 复核件亦明写「不变」）。本文件保留对摘要五字段的回归断言。
裁决 4：ttl 过期立场进重分配池（标 reassignable）而非静默当结论用。
裁决 1：ask 配额 N=100（(matter_id, actor, target) 滑窗超限 429）。

【2026-09-16 更正】裁决 1（ask 配额）与裁决 4（ttl 进重分配池）已被 owner
重新裁定推翻，两条对应的测试随之移除（红测试不留仓）。更正后的口径由
tests/api/test_rulings_2026_09_16.py 钉住：
- 裁决 1 → 定向提问不设上限（回退本次落地）；
- 裁决 4 → ttl 过期不进重分配池，改由发起人负责重新拉人 + 系统提醒，
  新实现由 r2EOiO 承载。
本文件不再包含这两条的断言。
"""

import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from tests.conftest import make_user


@pytest.fixture()
def users(db_session):
    return {n: make_user(db_session, n) for n in ("init", "alice", "bob")}


def test_irreversible变更必须带理由(db_session, users):
    with pytest.raises(ApiError) as exc:
        matter_svc.create_matter(
            db_session, initiator=users["init"], title="T", goal="G",
            background="B", participant_ids=[users["alice"].id,
                                             users["bob"].id],
            initiator_participates=False, timeout_seconds=3600,
            max_rounds=10, draft_questions=["Q?"],
            irreversible=True,  # 无 irreversible_reason
        )
    assert exc.value.status_code == 422


def test_irreversible变更带理由可建(db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"], irreversible=True,
        irreversible_reason="涉及生产数据删除，不可逆",
    )
    assert matter.irreversible is True


def test_can_commit开通限非终态事项(db_session, users):
    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter.status = "completed"
    db_session.commit()
    with pytest.raises(ApiError) as exc:
        matter_svc.set_agent_authority(
            db_session, matter_id=matter.id, user_id=users["alice"].id,
            agent_authority="can_commit",
        )
    assert exc.value.status_code == 409


def test_decide_item仅生成草稿_不可逆事项拒绝终裁(db_session, users):
    from hub.api import resolutions as res_svc

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"], irreversible=True,
        irreversible_reason="涉及生产数据删除",
    )
    with pytest.raises(ApiError) as exc:
        res_svc.decide_resolution(
            db_session, matter_id=matter.id, actor=users["init"],
            decision="approve", expected_version=1,
        )
    assert exc.value.status_code in (403, 409, 422)


def test_get_digest返回最新摘要与状态(db_session, users):
    from hub.db.models import Round, RoundSummary

    matter = matter_svc.create_matter(
        db_session, initiator=users["init"], title="T", goal="G",
        background="B", participant_ids=[users["alice"].id, users["bob"].id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=10,
        draft_questions=["Q?"],
    )
    matter_svc.start_matter(db_session, matter_id=matter.id, actor=users["init"])
    db_session.commit()
    rnd = db_session.scalar(select(Round).where(Round.matter_id == matter.id))
    db_session.add(RoundSummary(round_id=rnd.id, matter_id=matter.id,
                                consensus_points=["共识X"], divergences=[],
                                blind_spots=[], open_questions=[],
                                convergence="continue", generation_status="ok"))
    db_session.commit()

    from hub.mcp_server.methods import mcp_get_digest
    result = mcp_get_digest(db_session, None, user_id=users["init"].id,
                            matter_id=matter.id)
    assert result["status"] == matter.status
    assert result["consensus_points"] == ["共识X"]
    assert result["convergence"] == "continue"
