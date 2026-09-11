"""差异化分发：把同一份事实基座，向不同接收者产出不同的消息。

纯函数，零 IO —— 不调 LLM、不碰数据库、不读文件。
"""

from hub.domain.audience import (
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
        sections={"决定事项": "换不换供应商", "结论": "细节可以问 33 和 44"},
        visible_user_ids=[11],
        fact_version=fact.fact_version,
    )

    result = scan_view(fact, leaked)

    assert isinstance(result, ScanResult)
    assert result.blocked is True
    assert any("33" in violation for violation in result.violations)


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


def test_正常裁剪出来的视图不会被出口扫描误拦():
    fact = _fact(
        supporting_user_ids=[11, 44],
        opposing_user_ids=[22, 33],
        dissenting={22: "我认为应该直接换掉现供应商。", 33: "我担心切换期间对账会乱。"},
    )

    for viewer in (11, 22, 33, 44):
        result = scan_view(fact, build_audience_view(fact, viewer))
        assert result.blocked is False, result.violations


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


def test_反对者没留下具体意见时不编造一段出来():
    fact = _fact(opposing_user_ids=[22], dissenting={})

    view = build_audience_view(fact, 22)

    assert view.sections["你当初的意见"].strip()
    assert "没有记录" in view.sections["你当初的意见"]
