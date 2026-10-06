"""待办事项的 MCP 方法测试（v21，2026-10-07）。

这一组是owner 那个诉求的落点：「我想知道什么可以直接问 agent」。
所以测的不只是函数对不对，而是**返回值对 agent 来说够不够直接用** ——
完成率算好了吗？逾期标了吗？按人分组了吗？口径一致吗？

覆盖：list_todos / get_project_status / create_todo / update_todo，
含权限、状态机、口径边界（空板、dropped、AI 待确认）。
"""

import pytest

from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import MatterParticipant, Todo
from hub.domain.timeutil import utcnow
from hub.mcp_server.methods import (
    mcp_create_todo,
    mcp_get_project_status,
    mcp_list_todos,
    mcp_update_todo,
)
from tests.conftest import make_user


@pytest.fixture()
def alice(db_session):
    return make_user(db_session, "alice", "pw-123456")


@pytest.fixture()
def bob(db_session):
    return make_user(db_session, "bob", "pw-123456")


@pytest.fixture()
def carol(db_session):
    return make_user(db_session, "carol", "pw-123456")


@pytest.fixture()
def settings(settings):
    return settings


@pytest.fixture()
def board(db_session, alice, bob, carol):
    """一个留言板事项：alice 发起，bob 与 carol 参与。"""
    m = matter_svc.create_matter(
        db_session, initiator=alice, title="上线方案",
        goal="尽快上线", background="讨论方案",
        participant_ids=[bob.id, carol.id], initiator_participates=False,
        timeout_seconds=3600, max_rounds=5, draft_questions=["怎么看？"],
    )
    db_session.flush()
    return m


def _todo(db_session, *, board, title, **kwargs):
    payload = {"matter_id": board.id, "title": title,
               "status": "open", "source": "manual", "needs_confirm": False}
    payload.update(kwargs)
    t = Todo(**payload)
    db_session.add(t)
    db_session.flush()
    return t


# ---------------------------------------------------------------- create_todo


def test_建待办_成功(db_session, settings, board, alice):
    res = mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                          matter_id=board.id, title="写上线方案",
                          assignee_id=alice.id)

    assert res["title"] == "写上线方案"
    assert res["status"] == "open"
    assert res["matter_title"] == "上线方案"
    # agent 建的是人明确指示的，直接正式，不需要人再确认
    assert res["needs_confirm"] is False


def test_建待办_可暂不指派(db_session, settings, board, alice):
    res = mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                          matter_id=board.id, title="这个得有人跟")

    assert res["assignee"] is None


def test_建待办_标题为空报错(db_session, settings, board, alice):
    with pytest.raises(ApiError) as ei:
        mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                        matter_id=board.id, title="   ")
    assert ei.value.status_code == 400


def test_建待办_指派给非参与人被拒(db_session, settings, board, alice):
    outsider = make_user(db_session, "dave", "pw-123456")

    with pytest.raises(ApiError) as ei:
        mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                        matter_id=board.id, title="给外人", assignee_id=outsider.id)

    assert ei.value.status_code == 400


def test_建待办_非成员被拒404(db_session, settings, board, carol):
    """发起人之外的纯外人看不到这个板子。报404 而非 403，避免被拿去探测。"""
    outsider = make_user(db_session, "dave", "pw-123456")

    with pytest.raises(ApiError) as ei:
        mcp_create_todo(db_session, settings=settings, user_id=outsider.id,
                        matter_id=board.id, title="偷偷建一个")

    assert ei.value.status_code == 404


def test_建待办_事项不存在报404(db_session, settings, alice):
    with pytest.raises(ApiError) as ei:
        mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                        matter_id="mat-不存在", title="x")
    assert ei.value.status_code == 404


# ---------------------------------------------------------------- list_todos


def test_列待办_默认只给未完成的(db_session, settings, board, alice, bob):
    _todo(db_session, board=board, title="未完成的事", assignee_id=bob.id)
    _todo(db_session, board=board, title="已完成的事",
          assignee_id=bob.id, status="done")

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id)

    assert res["total"] == 1
    assert res["todos"][0]["title"] == "未完成的事"


def test_列待办_默认只看自己的(db_session, settings, board, bob, carol):
    _todo(db_session, board=board, title="bob的活", assignee_id=bob.id)
    _todo(db_session, board=board, title="carol的活", assignee_id=carol.id)

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id)

    assert [t["title"] for t in res["todos"]] == ["bob的活"]


def test_列待办_含事项名与剩余天数(db_session, settings, board, bob):
    """这两项 agent 最常要，但让它自己算容易错（取整/时区）。"""
    from datetime import timedelta

    _todo(db_session, board=board, title="三天后的事", assignee_id=bob.id,
          due_at=utcnow() + timedelta(days=3))

    item = mcp_list_todos(db_session, settings=settings,
                          user_id=bob.id)["todos"][0]

    assert item["matter_title"] == "上线方案"
    assert item["days_left"] is not None and 2 <= item["days_left"] <= 3
    assert item["overdue"] is False


def test_列待办_逾期会被标出并给提示(db_session, settings, board, bob):
    from datetime import timedelta

    _todo(db_session, board=board, title="早就该做了", assignee_id=bob.id,
          due_at=utcnow() - timedelta(days=5))
    _todo(db_session, board=board, title="正常的事", assignee_id=bob.id)

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id)

    assert res["overdue_count"] == 1
    assert res["hint"] is not None and "逾期" in res["hint"]
    overdue_item = [t for t in res["todos"] if t["overdue"]][0]
    assert overdue_item["title"] == "早就该做了"


def test_列待办_默认不带出无主待办(db_session, settings, board, bob):
    """默认视图是「我的活」，混进无主的会让 agent 汇报失焦。

    注意 ``include_unassigned=True`` 在``assignee=me`` 下也不改变结果 ——
    「我的活」就是只有我指派的，无主的本来就不属于它。无主待办要用
    ``assignee=none`` 查。
    """
    _todo(db_session, board=board, title="有主的", assignee_id=bob.id)
    _todo(db_session, board=board, title="没主的", assignee_id=None)

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id)
    assert [t["title"] for t in res["todos"]] == ["有主的"]

    #显式要「我 + 无主」时也不会有：无主不是「我的活」
    with_un = mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                             include_unassigned=True)
    assert [t["title"] for t in with_un["todos"]] == ["有主的"]

    # 要看全部（包括无主）用 assignee=all
    all_todos = mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                               assignee="all")
    assert {t["title"] for t in all_todos["todos"]} == {"有主的", "没主的"}


def test_列待办_无主视图(db_session, settings, board, bob):
    _todo(db_session, board=board, title="没主的", assignee_id=None)

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                         assignee="none")

    assert [t["title"] for t in res["todos"]] == ["没主的"]


def test_列待办_按事项过滤(db_session, settings, board, alice, bob, carol):
    other = matter_svc.create_matter(
        db_session, initiator=alice, title="别的事项",
        goal="g", background="b", participant_ids=[bob.id, carol.id],
        initiator_participates=False, timeout_seconds=3600, max_rounds=5,
        draft_questions=["q"],
    )
    db_session.flush()
    _todo(db_session, board=board, title="本板的", assignee_id=bob.id)
    _todo(db_session, board=other, title="别板的", assignee_id=bob.id)

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                         matter_id=board.id)

    assert [t["title"] for t in res["todos"]] == ["本板的"]


def test_列待办_指定无权限事项报404(db_session, settings, board, carol):
    _todo(db_session, board=board, title="x", assignee_id=carol.id)
    outsider = make_user(db_session, "dave", "pw-123456")

    with pytest.raises(ApiError) as ei:
        mcp_list_todos(db_session, settings=settings, user_id=outsider.id,
                       matter_id=board.id)

    assert ei.value.status_code == 404


def test_列待办_非法参数报400(db_session, settings, board, bob):
    with pytest.raises(ApiError):
        mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                       assignee="随便什么")
    with pytest.raises(ApiError):
        mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                       status="不存在")


def test_列待办_逾期宽限一天(db_session, settings, board, bob):
    """刚过截止时间不到一天不算逾期 —— 深夜 23:59 的任务标逾期是噪音。"""
    from datetime import timedelta

    _todo(db_session, board=board, title="今天刚到期", assignee_id=bob.id,
          due_at=utcnow() - timedelta(hours=6))

    res = mcp_list_todos(db_session, settings=settings, user_id=bob.id)

    assert res["overdue_count"] == 0


# -------------------------------------------------------- get_project_status


def test_项目状态_空板返回0而不是100(db_session, settings, board, alice):
    """空板显示「100% 完成」是错的。"""
    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["progress"]["completion_rate"] == 0
    assert res["progress"]["total"] == 0
    assert "0 项待办" in res["headline"]


def test_项目状态_完成率口径正确(db_session, settings, board, alice, bob):
    """已完成 1 / 未完成 3 = 25%。"""
    _todo(db_session, board=board, title="done1", assignee_id=bob.id, status="done")
    for i in range(3):
        _todo(db_session, board=board, title=f"open{i}", assignee_id=bob.id)

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["progress"]["total"] == 4
    assert res["progress"]["done"] == 1
    assert res["progress"]["pending"] == 3
    assert res["progress"]["completion_rate"] == 25


def test_项目状态_dropped不进分母(db_session, settings, board, alice, bob):
    """有意不做的不是「没做」，不该拉低完成率。"""
    _todo(db_session, board=board, title="a", assignee_id=bob.id, status="done")
    _todo(db_session, board=board, title="b", assignee_id=bob.id, status="done")
    _todo(db_session, board=board, title="不做了", assignee_id=bob.id,
          status="dropped")

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["progress"]["total"] == 2
    assert res["progress"]["completion_rate"] == 100


def test_项目状态_AI待确认不进分母(db_session, settings, board, alice, bob):
    """AI 抽的还没人确认，不该拉低完成率也不该算完成。"""
    _todo(db_session, board=board, title="a", assignee_id=bob.id, status="done")
    _todo(db_session, board=board, title="待确认", assignee_id=bob.id,
          status="done", needs_confirm=True, source="extracted_from_message")

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["progress"]["total"] == 1
    assert res["progress"]["completion_rate"] == 100
    assert res["progress"]["awaiting_confirm"] == 1
    assert "待确认" in res["headline"]


def test_项目状态_按人分组并标逾期(db_session, settings, board, alice, bob, carol):
    from datetime import timedelta

    _todo(db_session, board=board, title="bob逾期", assignee_id=bob.id,
          due_at=utcnow() - timedelta(days=5))
    _todo(db_session, board=board, title="bob正常", assignee_id=bob.id)
    _todo(db_session, board=board, title="carol的", assignee_id=carol.id,
          status="done")

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    by_name = {p["assignee"]: p for p in res["by_person"]}
    assert by_name["bob"]["open"] == 2
    assert by_name["bob"]["overdue"] == 1
    assert by_name["carol"]["done"] == 1
    # 逾期多的排前面 —— agent 汇报时先说有风险的
    assert res["by_person"][0]["assignee"] == "bob"


def test_项目状态_用板上称呼而不是username(db_session, settings, board,
                                          alice, bob):
    from sqlalchemy import update

    db_session.execute(
        update(MatterParticipant)
        .where(MatterParticipant.matter_id == board.id,
               MatterParticipant.user_id == bob.id)
        .values(display_name="Bob经理")
    )
    db_session.flush()
    _todo(db_session, board=board, title="x", assignee_id=bob.id)

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["by_person"][0]["assignee"] == "Bob经理"


def test_项目状态_风险取自总结的open_questions(db_session, settings, board, alice):
    from hub.db.models import BoardSummary

    db_session.add(BoardSummary(
        matter_id=board.id, summary="s", judgement="j",
        key_points=[], open_questions=["预算还没定", "谁来跟进"],
        covered_messages=0, generation_status="ok", updated_at=utcnow(),
    ))
    db_session.flush()

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["risks"] == ["预算还没定", "谁来跟进"]


def test_项目状态_未指派数单独统计(db_session, settings, board, alice, bob):
    _todo(db_session, board=board, title="有主", assignee_id=bob.id)
    _todo(db_session, board=board, title="无主", assignee_id=None)

    res = mcp_get_project_status(db_session, settings=settings,
                                 user_id=alice.id, matter_id=board.id)

    assert res["progress"]["unassigned_count"] == 1


def test_项目状态_非成员报404(db_session, settings, board, alice):
    outsider = make_user(db_session, "dave", "pw-123456")

    with pytest.raises(ApiError) as ei:
        mcp_get_project_status(db_session, settings=settings,
                               user_id=outsider.id, matter_id=board.id)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------- update_todo


def test_改待办_标记完成(db_session, settings, board, alice, bob):
    t = _todo(db_session, board=board, title="写方案", assignee_id=bob.id)

    res = mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                          todo_id=t.id, status="done")

    assert res["status"] == "done"


def test_改待办_非法转移被拒(db_session, settings, board, bob):
    """dropped → done 不该被静默接受。"""
    t = _todo(db_session, board=board, title="x", assignee_id=bob.id,
              status="dropped")

    with pytest.raises(ApiError) as ei:
        mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                        todo_id=t.id, status="done")

    assert ei.value.status_code == 400


def test_改待办_已完成的不再改动(db_session, settings, board, bob):
    """完成的待办是留痕，改了会让审计失真。"""
    t = _todo(db_session, board=board, title="x", assignee_id=bob.id,
              status="done")

    with pytest.raises(ApiError) as ei:
        mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                        todo_id=t.id, assignee_id=bob.id)

    assert ei.value.status_code == 400


def test_改待办_可以改回open再调(db_session, settings, board, bob):
    t = _todo(db_session, board=board, title="x", assignee_id=bob.id,
              status="done")

    mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                    todo_id=t.id, status="open")
    res = mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                          todo_id=t.id, status="done")

    assert res["status"] == "done"


def test_改待办_不能指派给非参与人(db_session, settings, board, bob):
    outsider = make_user(db_session, "dave", "pw-123456")
    t = _todo(db_session, board=board, title="x", assignee_id=bob.id)

    with pytest.raises(ApiError) as ei:
        mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                        todo_id=t.id, assignee_id=outsider.id)

    assert ei.value.status_code == 400


def test_改待办_不存在的报404(db_session, settings, board, bob):
    with pytest.raises(ApiError) as ei:
        mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                        todo_id="todo-不存在", status="done")
    assert ei.value.status_code == 404


def test_改待办_非成员报404(db_session, settings, board, bob):
    outsider = make_user(db_session, "dave", "pw-123456")
    t = _todo(db_session, board=board, title="x", assignee_id=bob.id)

    with pytest.raises(ApiError) as ei:
        mcp_update_todo(db_session, settings=settings, user_id=outsider.id,
                        todo_id=t.id, status="done")
    assert ei.value.status_code == 404


# ---------------------------------------------------------------- 组合场景


def test_端到端_问agent项目到哪了(db_session, settings, board, alice, bob, carol):
    """模拟真实提问：bob 建 3 条、完成 1 条，然后问整体进度。"""
    from datetime import timedelta

    made = [
        mcp_create_todo(db_session, settings=settings, user_id=alice.id,
                        matter_id=board.id, title=f"任务{i}", assignee_id=bob.id)
        for i in range(3)
    ]
    mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                    todo_id=made[0]["todo_id"], status="done")
    mcp_update_todo(db_session, settings=settings, user_id=bob.id,
                    todo_id=made[1]["todo_id"], status="doing",
                    due_at=(utcnow() - timedelta(days=3)).isoformat())

    mine = mcp_list_todos(db_session, settings=settings, user_id=bob.id,
                          # 逾期那条被设成了 doing，默认只看 open 看不到它
                          status="all")
    assert mine["overdue_count"] == 1

    whole = mcp_get_project_status(db_session, settings=settings,
                                   user_id=alice.id, matter_id=board.id)
    assert whole["progress"]["completion_rate"] == 33
    assert whole["progress"]["overdue_count"] == 1
    assert "33%" in whole["headline"]