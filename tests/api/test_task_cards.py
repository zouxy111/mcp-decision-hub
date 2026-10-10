"""任务卡测试（v22，2026-10-10）。

覆盖：领域纯函数、服务层（权限/状态机/软审查）、MCP 方法、REST 端点、
网页流程。AI 审查用 conftest 的 FakeLLM 桩。
"""

import pytest

from hub.api import matters as matter_svc
from hub.api import task_cards as svc
from hub.domain import task_cards as cards
from hub.mcp_server import methods as m
from tests.conftest import make_user


@pytest.fixture()
def alice(db_session):
    return make_user(db_session, "alice", "pw-123456")


@pytest.fixture()
def bob(db_session):
    return make_user(db_session, "bob", "pw-123456")


@pytest.fixture()
def outsider(db_session):
    return make_user(db_session, "outsider", "pw-123456")


@pytest.fixture()
def board(db_session, alice, bob):
    return matter_svc.create_board_matter(
        db_session, initiator=alice, title="协同交付",
        goal="把活交好", participant_ids=[bob.id])


CRITERIA = ["覆盖全部 21 项判断", "每项注明数据来源"]


def _publish(db_session, alice):
    card = svc.create_card(db_session, matter_id=board_id(db_session),
                           publisher=alice, title="问卷初稿",
                           description="完成初稿",
                           acceptance_criteria=CRITERIA)
    return svc.publish_card(db_session, card_id=card.id, actor=alice)


def board_id(db_session):
    from sqlalchemy import select
    from hub.db.models import Matter
    return db_session.scalar(select(Matter.id))


# --------------------------------------------------------------------------
# 领域纯函数
# --------------------------------------------------------------------------

def test_validate_criteria_去重去空白限条数():
    assert cards.validate_criteria(["", "  ", "A", "A", " B "]) == ["A", "B"]
    many = [f"第{i}条" for i in range(30)]
    assert len(cards.validate_criteria(many)) == cards.MAX_CRITERIA


def test_normalize_self_check_对齐标准补未自评():
    out = cards.normalize_self_check(
        [{"criterion": CRITERIA[1], "met": False, "note": "缺数据"}],
        CRITERIA)
    assert out[0] == {"criterion": CRITERIA[0], "met": None, "note": ""}
    assert out[1]["met"] is False and out[1]["note"] == "缺数据"


def test_review_overall():
    assert cards.review_overall([{"verdict": "pass"}]) == "pass"
    assert cards.review_overall([{"verdict": "pass"},
                                 {"verdict": "gap"}]) == "gaps"


# --------------------------------------------------------------------------
# 服务层
# --------------------------------------------------------------------------

def test_建卡_非成员404(db_session, board, outsider):
    with pytest.raises(svc.ApiError) as exc:
        svc.create_card(db_session, matter_id=board.id, publisher=outsider,
                        title="x")
    assert exc.value.status_code == 404


def test_建卡_标题必填(db_session, board, alice):
    with pytest.raises(svc.ApiError) as exc:
        svc.create_card(db_session, matter_id=board.id, publisher=alice,
                        title="  ")
    assert exc.value.status_code == 422


def test_发布_仅发布人且必须有验收标准(db_session, board, alice, bob):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="无标准的卡")
    with pytest.raises(svc.ApiError) as exc:  # 发布人是 bob → 403
        svc.publish_card(db_session, card_id=card.id, actor=bob)
    assert exc.value.status_code == 403
    with pytest.raises(svc.ApiError) as exc:  # 标准为空 → 422
        svc.publish_card(db_session, card_id=card.id, actor=alice)
    assert exc.value.status_code == 422


def test_发布后锁定不可改(db_session, board, alice):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="问卷初稿", acceptance_criteria=CRITERIA)
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    with pytest.raises(svc.ApiError) as exc:
        svc.update_card(db_session, card_id=card.id, actor=alice,
                        acceptance_criteria=["新口径"])
    assert exc.value.status_code == 409
    # 非发布人连 draft 都不能改
    card2 = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                            title="第二张")
    with pytest.raises(svc.ApiError) as exc2:
        svc.update_card(db_session, card_id=card2.id,
                        actor=make_user(db_session, "c", "pw-123456"),
                        title="抢改")
    assert exc2.value.status_code in (403, 404)


def test_交付_仅已发布卡(db_session, board, alice, bob):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="问卷初稿", acceptance_criteria=CRITERIA)
    with pytest.raises(svc.ApiError) as exc:
        svc.submit_delivery(db_session, card_id=card.id, user=bob,
                            summary="初稿完成")
    assert exc.value.status_code == 409
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    d = svc.submit_delivery(db_session, card_id=card.id, user=bob,
                            summary="21 项全做完",
                            self_check=[{"criterion": CRITERIA[0],
                                         "met": True}])
    assert d.status == "submitted"
    checks = cards.parse_self_check(d.self_check)
    assert checks[0]["met"] is True and checks[1]["met"] is None
    assert d.review_status == "failed"  # 无 llm → 软审查失败但不阻断


def test_AI审查_桩返回verdicts(db_session, board, alice, bob, make_fake_llm):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="问卷初稿", acceptance_criteria=CRITERIA)
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    llm = make_fake_llm([{
        "verdicts": [
            {"criterion": CRITERIA[0], "verdict": "pass"},
            {"criterion": CRITERIA[1], "verdict": "gap",
             "gap": "第 7 项未注明来源"},
        ],
        "overall": "gaps",
    }])
    d = svc.submit_delivery(db_session, card_id=card.id, user=bob,
                            summary="做完了", llm=llm)
    assert d.review_status == "gaps"
    review = cards.parse_ai_review(d.ai_review)
    assert review["verdicts"][1]["verdict"] == "gap"
    assert llm.calls[0]["schema_name"] == "task_delivery_review"


def test_AI审查_LLm异常仍落库(db_session, board, alice, bob, make_fake_llm):
    from hub.llm.client import LLMError
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="卡", acceptance_criteria=CRITERIA)
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    llm = make_fake_llm([LLMError("LLM_TIMEOUT", "超时", retry_count=3)])
    d = svc.submit_delivery(db_session, card_id=card.id, user=bob,
                            summary="做完了", llm=llm)
    assert d.review_status == "failed" and d.ai_review is None
    assert d.status == "submitted"


def test_验收_仅发布人可决(db_session, board, alice, bob):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="卡", acceptance_criteria=CRITERIA)
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    d = svc.submit_delivery(db_session, card_id=card.id, user=bob,
                            summary="交活")
    with pytest.raises(svc.ApiError) as exc:
        svc.decide_delivery(db_session, delivery_id=d.id, actor=bob,
                            accept=True)
    assert exc.value.status_code == 403
    d2 = svc.decide_delivery(db_session, delivery_id=d.id, actor=alice,
                             accept=False, note="来源没标全")
    assert d2.status == "rejected" and d2.reviewer_note == "来源没标全"
    # 已决不能再决
    with pytest.raises(cards.TaskCardError):
        svc.decide_delivery(db_session, delivery_id=d.id, actor=alice,
                            accept=True)


# --------------------------------------------------------------------------
# MCP 方法（单一事实源出口）
# --------------------------------------------------------------------------

def test_MCP_任务卡全流程(db_session, board, alice, bob):
    from hub.config import load_settings
    settings = load_settings()
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="问卷初稿", acceptance_criteria=CRITERIA)
    out = m.mcp_list_task_cards(db_session, settings, user_id=bob.id,
                                matter_id=board.id)
    assert out["cards"][0]["status"] == "draft"
    assert out["cards"][0]["acceptance_criteria"] == CRITERIA

    svc.publish_card(db_session, card_id=card.id, actor=alice)
    view = m.mcp_get_task_card(db_session, settings, user_id=bob.id,
                               card_id=card.id)
    assert view["status"] == "published"

    d = m.mcp_submit_delivery(db_session, settings, user_id=bob.id,
                              card_id=card.id, summary="做完了",
                              self_check=[{"criterion": CRITERIA[0],
                                           "met": True}])
    assert d["status"] == "submitted" and d["review_status"] == "failed"
    view = m.mcp_get_task_card(db_session, settings, user_id=alice.id,
                               card_id=card.id)
    assert view["deliveries"][0]["submitter"] == "bob"


# --------------------------------------------------------------------------
# REST 端点
# --------------------------------------------------------------------------

def _token(client, db_session, user):
    from hub.api.tokens import issue_token
    _, plain = issue_token(db_session, user=user, name="t")
    db_session.commit()
    return plain


def test_REST_任务卡端点(client, db_session, board, alice, bob):
    card = svc.create_card(db_session, matter_id=board.id, publisher=alice,
                           title="问卷初稿", acceptance_criteria=CRITERIA)
    svc.publish_card(db_session, card_id=card.id, actor=alice)
    db_session.commit()
    tok = _token(client, db_session, bob)
    headers = {"Authorization": f"Bearer {tok}"}

    r = client.get(f"/api/items/{board.id}/task_cards", headers=headers)
    assert r.status_code == 200 and r.json()["cards"][0]["card_id"] == card.id

    r = client.get(f"/api/items/task_cards/{card.id}", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "published"

    r = client.post(f"/api/items/task_cards/{card.id}/deliveries",
                    headers=headers, json={"summary": "REST 交付"})
    assert r.status_code == 201
    assert r.json()["status"] == "submitted"

    # 未带令牌 → 401
    assert client.get(f"/api/items/{board.id}/task_cards").status_code == 401


# --------------------------------------------------------------------------
# 网页流程
# --------------------------------------------------------------------------

def _login(client, username):
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": "pw-123456"})
    assert resp.status_code == 200


def test_网页_建卡发布交活验收(db_session, client, board, alice, bob):
    _login(client, "alice")
    r = client.post(f"/matters/{board.id}/cards",
                    data={"title": "问卷初稿", "description": "做完它",
                          "criteria_text": "标准一\n标准二"},
                    follow_redirects=False)
    assert r.status_code == 303
    from hub.db.models import TaskCard
    card = db_session.query(TaskCard).one()
    assert "/cards/" in r.headers["location"]

    # 详情页出现任务卡入口
    page = client.get(f"/matters/{board.id}")
    assert "任务卡" in page.text and "问卷初稿" in page.text

    # 卡片页可渲染
    page = client.get(f"/matters/{board.id}/cards/{card.id}")
    assert page.status_code == 200 and "标准一" in page.text

    # 发布
    r = client.post(f"/matters/{board.id}/cards/{card.id}/publish",
                    follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert card.status == "published"

    # bob 网页交活（勾选一条标准）
    _login(client, "bob")
    r = client.post(f"/matters/{board.id}/cards/{card.id}/deliveries",
                    data={"summary": "做完了", "met_criteria": ["标准一"]},
                    follow_redirects=False)
    assert r.status_code == 303
    from hub.db.models import TaskDelivery
    d = db_session.query(TaskDelivery).one()
    checks = cards.parse_self_check(d.self_check)
    assert checks[0]["met"] is True and checks[1]["met"] is None

    # 发布人验收通过
    _login(client, "alice")
    r = client.post(f"/deliveries/{d.id}/decide",
                    data={"decision": "accept"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert d.status == "accepted"

    # 卡片页展示验收结论
    page = client.get(f"/matters/{board.id}/cards/{card.id}")
    assert "已验收通过" in page.text
