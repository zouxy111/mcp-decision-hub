"""僵持判定纯函数（rUdiTJ · PRD-08 片 2/4 的判定部分）。

口径：「新增信息」= 本轮立场集合相对上轮有内容差异（见 `hub/domain/stall_detect.py`
的信号清单）。**刻意不计入**措辞与把握度数值的变化 —— 那两类只要模型重新生成
一次就会变，算进去会让判定永不触发。
"""

import pytest

from hub.domain.stall_detect import (
    CONDITIONS_CHANGED,
    NEGOTIABLES_CHANGED,
    NEW_STANCE,
    OPEN_QUESTIONS_CHANGED,
    QUESTIONS_FOR_CHANGED,
    STANCE_CHANGED,
    STANCE_REMOVED,
    StanceSnapshot,
    detect_stall,
)


def _snap(uid, stance="support", **kw):
    return StanceSnapshot(user_id=uid, stance=stance, **kw)


def _as_dict(*snaps):
    return {s.user_id: s for s in snaps}


def test_两轮完全一致判僵持():
    """⭐ 验收核心：内容完全一致 → 无新增信息。"""
    prev = _as_dict(_snap(1), _snap(2, "oppose"))
    curr = _as_dict(_snap(1), _snap(2, "oppose"))

    verdict = detect_stall(prev, curr)
    assert verdict.stalled is True
    assert verdict.signals == ()


def test_新增参与人立场算新信息():
    verdict = detect_stall(_as_dict(_snap(1)),
                           _as_dict(_snap(1), _snap(2)))
    assert verdict.stalled is False
    assert NEW_STANCE in verdict.signals


def test_立场取消失新信息():
    """有人撤了立场也是内容差异 —— 当「无新信息」会误判僵持。"""
    verdict = detect_stall(_as_dict(_snap(1), _snap(2)),
                           _as_dict(_snap(1)))
    assert verdict.stalled is False
    assert STANCE_REMOVED in verdict.signals


@pytest.mark.parametrize("field,expected", [
    ("stance", STANCE_CHANGED),
    ("non_negotiables", NEGOTIABLES_CHANGED),
    ("conditions", CONDITIONS_CHANGED),
    ("open_questions", OPEN_QUESTIONS_CHANGED),
    ("questions_for", QUESTIONS_FOR_CHANGED),
])
def test_五类字段变更各自成信号(field, expected):
    prev = _as_dict(_snap(1, **({field: ("旧",)} if field != "stance"
                                 else {"stance": "support"})))
    curr = _as_dict(_snap(1, **({field: ("新",)} if field != "stance"
                                 else {"stance": "oppose"})))
    verdict = detect_stall(prev, curr)
    assert verdict.stalled is False
    assert expected in verdict.signals


def test_集合顺序与重复不算差异():
    """同一批条件换个次序、或重复写一遍，不是「新信息」。

    ⚠️ **这条为什么刻意用 6 个元素 + 反向输入**：只断言「两种输入相等」是不够的。
    归一化里若挪掉 `sorted()`，两个 `tuple(set(...))` 的迭代顺序**受插入顺序影响**，
    有时相同、有时不同 → 测试变成**随机失败**，而随机失败在单机跑一次时看不出来。
    实测踩过：同一个提交、同一份 `uv.lock`，一台机器 `714 passed`、
    另一台 `713 passed + 1 failed`。

    元素给到 6 个并反序输入后，`set` 迭代顺序**恰好等于**排序结果的概率极低
    （1/720 量级），所以这条断言能把「挪掉 sorted」稳定地抓住。

    注意：断言的是 `sorted(keys)` 而**不是** `keys` —— 中文串按码位排序，
    与声明顺序不同（丁<丙<乙<己<戊<甲）。
    """
    keys = ("甲", "乙", "丙", "丁", "戊", "己")
    prev = _as_dict(_snap(1, conditions=keys))
    curr = _as_dict(_snap(1, conditions=tuple(reversed(keys)) + (keys[0],)))

    assert detect_stall(prev, curr).stalled is True
    # 归一化结果必须是**确定值**（排序后的字面量），而不是 set 的任意迭代顺序
    assert _snap(1, conditions=tuple(reversed(keys))).conditions == \
        tuple(sorted(keys))


def test_凭空多一条未决问题就算新信息():
    prev = _as_dict(_snap(1, open_questions=("甲",)))
    curr = _as_dict(_snap(1, open_questions=("甲", "乙")))
    verdict = detect_stall(prev, curr)
    assert verdict.stalled is False


def test_本轮一条立场都没有时不判僵持():
    """空数据不判僵持：拿不到立场说不出「没有新信息」，宁可多开一轮。

    这条同时保护既有行为 —— 既有用例里存在「轮次没有任何立场」的构造，
    若在此判僵持会把正常推进路径一起改掉。
    """
    assert detect_stall({}, {}).stalled is False
    assert detect_stall(_as_dict(_snap(1)), {}).stalled is False


def test_from_row_归一化questions_for():
    class _Row:
        user_id = 7
        stance = "conditional"
        non_negotiables = None
        conditions = ["b", "a", "a"]
        open_questions = []
        questions_for = [{"participant_id": "9", "question": "你的看法？"},
                         {"participant_id": "9", "question": "你的看法？"},
                         {"garbage": 1}]

    snap = StanceSnapshot.from_row(_Row())
    assert snap.conditions == ("a", "b")
    assert snap.questions_for == ("9:你的看法？",)
    assert snap.non_negotiables == ()


def test_verdict给出可读原因():
    assert detect_stall({}, {}).reason != ""
    stalled = detect_stall(_as_dict(_snap(1)), _as_dict(_snap(1)))
    assert "无任何内容差异" in stalled.reason
