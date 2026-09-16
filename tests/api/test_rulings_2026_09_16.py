"""2026-09-16 owner 更正裁定落地（TDD）。

背景：2026-09-14 的《裁决答复》有三处口径与落地代码不一致（ask 配额 /
roR8Pk 第 1 条 / ttl 过期归属）。owner 于 2026-09-16 重新裁定，本文件钉住
**更正后**的口径。被推翻的两条测试已从 test_rulings_2026_09_14.py 移除
——红测试不留仓，改用本文件的基线来钉。

裁定 1（更正）：定向提问（questions_for）**不设配额**。
    09-14 落地为 (matter, actor, target) 滑窗 N=100 超限 429；owner 维持
    「不设上限」，相关代码回退。

裁定 3（补定）：ttl 过期立场**不进重分配池**。
    09-14 的《裁决答复》根本没答这个问题，落地代码却把未决事项追认成
    「owner 裁决 B 进重分配池」。owner 于 09-16 补定：改由发起人负责重新
    拉人 + 系统提醒，系统不自动换人，重分配池出口删除。新实现（含提醒）
    由 r2EOiO 承载，不在本文件范围内。
"""

from hub.api.tokens import issue_token
from hub.db.models import Matter, MatterParticipant
from hub.domain.digest import compute_stance_content_hash
from tests.conftest import make_user


def _matter_with(db_session, initiator, *participants) -> Matter:
    """发起人本人不参与的事项。"""
    matter = Matter(initiator_id=initiator.id, title="T", goal="G",
                    background="B", initiator_participates=False)
    db_session.add(matter)
    db_session.flush()
    for participant in participants:
        db_session.add(MatterParticipant(matter_id=matter.id,
                                         user_id=participant.id))
    db_session.flush()
    return matter


def _headers(db_session, user, name) -> dict:
    _token, plaintext = issue_token(db_session, user=user, name=name)
    db_session.commit()
    return {"Authorization": f"Bearer {plaintext}"}


def _ask_payload(*, round_number: int, target_id: int) -> dict:
    data = {
        "round_number": round_number,
        "stance": "support",
        "confidence": 0.6,
        "position_summary": "支持",
        "rationale_summary": "需要对方补充数据",
        "non_negotiables": [],
        "conditions": [],
        "open_questions": [],
        "depends_on": [],
        "questions_for": [{"participant_id": str(target_id),
                           "question": "上线窗口是哪天？"}],
        "disagreement_kind": None,
        "supersedes": None,
        "acting_as": "human",
        "authority": None,
        "ttl_seconds": None,
        "urgency": "normal",
        "visibility": "participants",
    }
    data["content_hash"] = compute_stance_content_hash(data)
    return data


def test_定向提问不设配额_超过历史阈值仍可提交(client, db_session):
    """同一 (事项, 提问人, 被问人) 连续定向提问，不受次数上限约束。

    连续提交 101 次 —— 101 就是 09-14 误落地的配额阈值 + 1。那条上限若
    还在，第 101 次必然 429；裁定回退后应当全部落库。
    """
    alice = make_user(db_session, "alice")
    bob = make_user(db_session, "bob")
    carol = make_user(db_session, "carol")
    matter = _matter_with(db_session, carol, alice, bob)
    db_session.commit()
    headers = _headers(db_session, alice, "alice")

    denied = []
    for n in range(1, 102):
        resp = client.post(
            f"/api/items/{matter.id}/stances",
            json=_ask_payload(round_number=n, target_id=bob.id),
            headers=headers,
        )
        if resp.status_code != 201:
            denied.append((n, resp.status_code, resp.json()))

    assert denied == [], f"定向提问被拒：{denied[:3]}"


def test_ttl过期不产生重分配池出口():
    """ttl 过期改由发起人负责重新拉人，系统不提供自动换人入口。

    这是一个「退场守卫」：钉住被裁掉的出口不再回来。
    过期立场仍会被排除在收敛判定之外（649be9c 已落地），那是另一回事，
    与本条无关。
    """
    from hub.api import stances as stance_svc

    assert not hasattr(stance_svc, "list_reassignable_expired"), (
        "重分配池出口仍在：ttl 过期的归属已改判为「发起人负责拉人 + "
        "系统提醒」，系统不应再暴露自动换人入口"
    )
