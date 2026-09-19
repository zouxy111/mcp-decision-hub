"""差异化分发：把同一份事实基座，向不同接收者产出不同的消息。

纯函数，零 IO —— 不调 LLM、不碰数据库、不读文件。
"""

import re

from hub.domain.audience import (
    SCAN_LIMITATIONS,
    AudienceView,
    FactBase,
    ScanResult,
    build_audience_view,
    scan_view,
)


def _fact(**overrides) -> FactBase:
    data = {
        "fact_version": "v7",
        "item_title": "是否把结算系统换成新供应商",
        "question": "要不要在下个季度切换结算供应商？",
        "decision": "先不切换，继续用现在的供应商，但要求对方下个月给出降价方案。",
        "rationale": "现供应商的稳定性已经验证过，换供应商的迁移风险太大，而对方愿意谈价。",
        "supporting_user_ids": [11],
        "opposing_user_ids": [22],
        "risks": ["对方可能拖到下个季度才给方案"],
        "action_items": {11: ["跟进对方的降价方案"], 22: ["把这次没被采纳的理由记下来"]},
        "dissenting": {22: "我认为应该直接换掉现供应商，他们的报价比市场价高出两成。"},
    }
    data.update(overrides)
    return FactBase(**data)


def test_两个接收者拿到内容不同的视图():
    fact = _fact()

    supporter = build_audience_view(fact, 11)
    opposer = build_audience_view(fact, 22)

    assert supporter.sections != opposer.sections


def test_两个接收者拿到的事实版本完全一致():
    fact = _fact(fact_version="v7")

    supporter = build_audience_view(fact, 11)
    opposer = build_audience_view(fact, 22)

    assert supporter.fact_version == opposer.fact_version == "v7"


def test_支持者视图能看到自己的意见被采纳():
    supporter = build_audience_view(_fact(), 11)

    assert "你的意见被采纳了" in supporter.sections
    assert "采纳" in supporter.sections["你的意见被采纳了"]


def test_反对者视图包含为何未被采纳的三段():
    opposer = build_audience_view(_fact(), 22)

    assert opposer.sections["你当初的意见"] == (
        "我认为应该直接换掉现供应商，他们的报价比市场价高出两成。"
    )
    assert "现供应商的稳定性" in opposer.sections["为什么这次没有采纳"]
    assert "重新" in opposer.sections["什么情况下会重新考虑"]


def test_不在可见名单里的人其立场细节不出现():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22, 33],
        dissenting={
            22: "我认为应该直接换掉现供应商，他们的报价比市场价高出两成。",
            33: "我担心切换期间对账会乱，建议再多压对方一个月的账期。",
        },
        action_items={11: ["跟进对方的降价方案"], 33: ["去核对切换期间的对账流程"]},
    )

    view = build_audience_view(fact, 11)
    text = "\n".join(view.sections.values())

    assert view.visible_user_ids == [11]
    for uid, words in fact.dissenting.items():
        if uid not in view.visible_user_ids:
            assert words not in text

    # 本人自己的异议，只有本人看得到
    own = build_audience_view(fact, 22)
    assert fact.dissenting[22] in "\n".join(own.sections.values())


def test_出口扫描拦住混进来的其他用户id():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22, 33],
        dissenting={22: "我认为应该直接换掉现供应商。", 33: "我担心切换期间对账会乱。"},
    )
    leaked = AudienceView(
        viewer_user_id=11,
        sections={"决定事项": "换不换供应商", "结论": "细节可以问用户 33 和用户 44"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, leaked)

    assert isinstance(result, ScanResult)
    assert result.blocked is True
    assert len(result.violations) == 2  # 33、44 各一条

    # 反向：同一个事实基座下，裸整数不是 id 形态，不得判成泄漏。
    # 这条同时锁住「收窄过头」和「没收窄」两个方向。
    bare = AudienceView(
        viewer_user_id=11,
        sections={"决定事项": "换不换供应商", "结论": "细节可以问 33 和 44"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    assert scan_view(fact, bare).blocked is False


def test_以id形态出现的可见者不会被出口扫描误拦():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22, 33],
        dissenting={22: "我认为应该直接换掉现供应商。", 33: "我担心切换期间对账会乱。"},
    )
    view = AudienceView(
        viewer_user_id=11,
        sections={"结论": "细节可以问用户 11"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, view)

    assert result.blocked is False
    assert result.violations == []


def test_违规文案用显示名指代人而不是裸的用户id():
    fact = _fact(
        supporting_user_ids=[11],
        opposing_user_ids=[33],
        action_items={33: ["去核对切换期间的对账流程"]},
        dissenting={33: "我担心切换期间对账会乱。"},
        display_names={33: "carol"},
    )
    view = AudienceView(
        viewer_user_id=11,
        sections={"结论": "结论有疑问可以问用户 33"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, view)

    assert result.blocked is True
    assert "carol" in result.violations[0]
    assert "33" not in result.violations[0]


def test_缺失显示名时文案回退为未命名成员且不回显裸id():
    fact = _fact(
        supporting_user_ids=[11],
        opposing_user_ids=[33],
        action_items={33: ["去核对切换期间的对账流程"]},
        dissenting={33: "我担心切换期间对账会乱。"},
    )
    view = AudienceView(
        viewer_user_id=11,
        sections={"结论": "结论有疑问可以问用户 33"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, view)

    assert result.blocked is True
    assert "未命名成员" in result.violations[0]
    assert "33" not in result.violations[0]


def test_不传显示名也能构造事实基座():
    """向后兼容：display_names 是可选参数，存量构造点不受影响。"""
    fact = _fact()

    assert fact.display_names == {}


def test_出口扫描拦住混进来的他人异议原文():
    fact = _fact(
        opposing_user_ids=[22, 33],
        dissenting={22: "我认为应该直接换掉现供应商。", 33: "我担心切换期间对账会乱。"},
    )
    leaked = AudienceView(
        viewer_user_id=11,
        sections={"结论": "有人提出：我担心切换期间对账会乱。"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, leaked)

    assert result.blocked is True


def test_出口扫描拦住调用方指定的敏感文本():
    fact = _fact()
    view = AudienceView(
        viewer_user_id=11,
        sections={"结论": "对方内部代号叫蓝鸟，别往外说。"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, view, forbidden_terms=["蓝鸟"])

    assert result.blocked is True


def test_出口扫描随结果返回自己的能力边界():
    fact = _fact()
    view = build_audience_view(fact, 11)

    result = scan_view(fact, view)

    assert result.limitations == SCAN_LIMITATIONS
    assert len(result.limitations) == 3
    # B1 的取舍写进对外契约：裸整数不判泄漏，且必须让调用方知道。
    assert "裸整数" in result.limitations[1]
    assert all(item.strip() for item in result.limitations)


def test_正常裁剪出来的视图不会被出口扫描误拦():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22, 33],
        dissenting={22: "我认为应该直接换掉现供应商。", 33: "我担心切换期间对账会乱。"},
    )

    for viewer in (11, 22, 33, 44):
        result = scan_view(fact, build_audience_view(fact, viewer))
        assert result.blocked is False, result.violations


def test_业务正文里的裸整数不再被当成泄漏的用户id():
    """假阳性回归：标题里的 v2、正文里的「3 个方案」，都不是用户 id。"""
    fact = _fact(
        item_title="是否上线推荐系统 v2",
        rationale="先评估 3 个方案，再看 2 阶段",
        supporting_user_ids=[1],
        opposing_user_ids=[2],
        action_items={1: ["把评估结果整理成一页纸"]},
        dissenting={},
    )

    view = build_audience_view(fact, 1)
    text = "\n".join(view.sections.values())

    # 1) 拼出来的文本里确实有数字，否则这个回归根本测不到东西
    assert "v2" in text
    assert "3 个方案" in text
    # 2) 确实存在一个等于参与人 id 的裸整数（参与人 id 是 1/2）
    assert re.search(r"(?<!\d)2(?!\d)", text) is not None
    # 3) 兜底扫描不得把业务正文里的裸整数判成泄漏
    result = scan_view(fact, view)

    assert result.blocked is False
    assert result.violations == []


def test_个人行动项只出现在本人的视图里():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22],
        action_items={
            11: ["跟进对方的降价方案"],
            22: ["把这次没被采纳的理由记下来"],
        },
    )

    mine = build_audience_view(fact, 11)
    other = build_audience_view(fact, 22)

    assert "跟进对方的降价方案" in mine.sections["你要做的事"]
    assert "跟进对方的降价方案" not in "\n".join(other.sections.values())

    assert "把这次没被采纳的理由记下来" in other.sections["你要做的事"]
    assert "把这次没被采纳的理由记下来" not in "\n".join(mine.sections.values())


def test_结论非空时仍然解释为什么这么定():
    """防回归：这一节不能被整个删掉，只能按有没有结论来收放。"""
    fact = _fact()

    view = build_audience_view(fact, 11)

    assert view.sections["为什么这么定"] == fact.rationale


def test_还没有结论时不对反对者说为什么没有采纳():
    """结论都没定，就不能说「为什么这次没有采纳」。"""
    fact = _fact(decision=None, opposing_user_ids=[22])

    view = build_audience_view(fact, 22)

    assert view.sections["结论"] == "这事目前还没定下来。"
    assert "为什么这次没有采纳" not in view.sections
    assert view.sections["现在还没有结论"] == "这事目前还没定下来，你的意见还在桌面上。"


def test_还没有结论时不解释为什么这么定():
    """同上一条同类缺陷：没结论就不能紧接着解释「为什么这么定」。"""
    fact = _fact(decision=None)

    view = build_audience_view(fact, 11)

    assert view.sections["结论"] == "这事目前还没定下来。"
    assert "为什么这么定" not in view.sections


def test_反对者没留下具体意见时不编造一段出来():
    fact = _fact(opposing_user_ids=[22], dissenting={})

    view = build_audience_view(fact, 22)

    assert view.sections["你当初的意见"].strip()
    assert "没有记录" in view.sections["你当初的意见"]
