from hub.domain.convergence_eval import (
    UNDETECTED_CONDITIONAL_CONFLICT,
    UNDETECTED_NON_NEGOTIABLES,
    StanceInput,
    evaluate_convergence,
)


def test_两人都支持时达成收敛且无分歧():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="support", confidence=0.8),
        ]
    )
    assert result.converged is True
    assert result.divergences == []


def test_一人支持一人强烈反对时未收敛且分歧中能认出双方():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="oppose", confidence=0.8),
        ]
    )
    assert result.converged is False
    assert result.blocking
    assert any(
        set(d.user_ids) == {"alice", "bob"} for d in result.divergences
    ), "分歧应能识别出对立双方"


def test_风险偏好类分歧未收敛且标注补信息无用只能拉人():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(
                user_id="bob",
                stance="support",
                confidence=0.9,
                disagreement_kind="risk_appetite",
            ),
        ]
    )
    assert result.converged is False
    risk = [d for d in result.divergences if d.kind == "risk_appetite"]
    assert risk, "风险偏好分歧应出现在 divergences 里"
    assert any(
        "补信息无用" in d.description and "只能拉人" in d.description
        for d in risk
    ), "风险偏好分歧必须标注为补信息无用、只能拉人"


def test_有人弃权不阻断收敛但必须出现在阻塞点里():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="abstain", confidence=0.5),
        ]
    )
    assert result.converged is True, "弃权不应阻断收敛"
    assert any("bob" in b for b in result.blocking), "弃权者必须被标出，不能静默当成同意"


def test_有人不可谈判项与结论冲突时即使其余人全支持也未收敛():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="carol", stance="support", confidence=0.9),
            StanceInput(
                user_id="bob",
                stance="conditional",
                confidence=0.9,
                non_negotiables=("预算不得超过 100 万",),
            ),
        ]
    )
    assert result.converged is False
    assert any(
        "bob" in b and "预算不得超过 100 万" in b for b in result.blocking
    ), "不可谈判项冲突必须点名到人和具体约束"


def test_空输入与单人输入不崩溃并返回明确状态():
    empty = evaluate_convergence([])
    assert empty.converged is False
    assert empty.blocking, "空输入必须给出明确阻塞说明"
    assert empty.agreement_score == 0.0

    single = evaluate_convergence(
        [StanceInput(user_id="solo", stance="support", confidence=0.9)]
    )
    assert single.converged is True
    assert single.factions, "单人输入也应给出分组"


def test_需要补事实信息时未收敛且归类为补信息能解决():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(
                user_id="bob",
                stance="need_info",
                confidence=0.6,
                disagreement_kind="fact",
            ),
        ]
    )
    assert result.converged is False
    fact = [d for d in result.divergences if d.kind == "fact"]
    assert fact, "事实类待补信息应出现在 divergences 里"
    assert any("补信息能解决" in d.description for d in fact)
    assert any("bob" in b and "补信息" in b for b in result.blocking)


def test_低置信度反对仍算未收敛且必须给出阻塞点():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="oppose", confidence=0.4),
        ]
    )
    assert result.converged is False
    assert result.blocking, "未收敛必须始终给出至少一个阻塞点，供调用方解释"


def test_宽容版跳过未知立场并标记降级():
    from hub.domain.convergence_eval import evaluate_convergence_lenient

    result = evaluate_convergence_lenient(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="banana", confidence=0.9),
        ]
    )
    assert result.degraded is True
    assert result.skipped_stance_user_ids == ("bob",)
    assert result.converged is False
    assert result.agreement_score == 1.0, "只应统计保留下来的 alice"
    blob = " ".join(result.blocking) + " ".join(
        d.description for d in result.divergences
    )
    assert "banana" not in blob, "被跳过的脏数据不得泄漏进任何给调用方的文本"


def test_宽容版降级后即使其余人全支持也不许判收敛():
    from hub.domain.convergence_eval import evaluate_convergence_lenient

    result = evaluate_convergence_lenient(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="carol", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="banana", confidence=0.9),
        ]
    )
    assert result.agreement_score == 1.0, "脏数据不参与打分，其余两人全支持"
    assert result.degraded is True
    assert result.converged is False, "降级状态本身即阻断收敛，脏数据不许冒充共识"


def test_严格版遇到未知立场仍然抛错():
    import pytest

    from hub.domain.convergence_eval import ConvergenceEvalError

    with pytest.raises(ConvergenceEvalError):
        evaluate_convergence(
            [
                StanceInput(user_id="alice", stance="support", confidence=0.9),
                StanceInput(user_id="bob", stance="banana", confidence=0.9),
            ]
        )


def test_不可谈判项与结论的比对未实现时如实标注为未检测():
    stances = [
        StanceInput(user_id="alice", stance="support", confidence=0.9),
        StanceInput(
            user_id="bob",
            stance="conditional",
            confidence=0.9,
            non_negotiables=("预算不得超过 100 万",),
        ),
    ]
    result = evaluate_convergence(stances)
    assert UNDETECTED_NON_NEGOTIABLES in result.undetected_checks, (
        "非支持者的不可谈判项本轮无法与结论文本比对，必须如实标注未检测"
    )


def test_传入结论文本不改变任何判定结果():
    stances = [
        StanceInput(user_id="alice", stance="support", confidence=0.9),
        StanceInput(
            user_id="bob",
            stance="conditional",
            confidence=0.9,
            non_negotiables=("预算不得超过 100 万",),
        ),
    ]
    without = evaluate_convergence(stances)
    with_text = evaluate_convergence(stances, decision="决定采用方案 A，预算 150 万")

    assert with_text.converged is False
    assert with_text.converged == without.converged
    assert with_text.blocking == without.blocking, (
        "本版不做语义比对：结论文本不得影响阻塞点"
    )
    assert UNDETECTED_NON_NEGOTIABLES in with_text.undetected_checks


def test_支持者的不可谈判项不算未检测项():
    result = evaluate_convergence(
        [
            StanceInput(
                user_id="alice",
                stance="support",
                confidence=0.9,
                non_negotiables=("预算不得超过 100 万",),
            ),
        ]
    )
    assert UNDETECTED_NON_NEGOTIABLES not in result.undetected_checks


def test_宽容版同样接受decision参数并暴露未检测项():
    from hub.domain.convergence_eval import evaluate_convergence_lenient

    result = evaluate_convergence_lenient(
        [
            StanceInput(
                user_id="bob",
                stance="oppose",
                confidence=0.5,
                non_negotiables=("不加班",),
            )
        ],
        decision="随便一段结论文本",
    )
    assert result.degraded is False
    assert UNDETECTED_NON_NEGOTIABLES in result.undetected_checks


def test_有条件却无法检测条件间冲突时如实标注未检测():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(
                user_id="bob",
                stance="conditional",
                confidence=0.9,
                conditions=("预算不超过 100 万", "不得裁撤现有团队"),
            ),
        ]
    )
    assert UNDETECTED_CONDITIONAL_CONFLICT in result.undetected_checks, (
        "条件冲突本轮无法检测，必须如实暴露，不能让调用方以为已核对"
    )
    assert all(
        "no_conflict" not in name and "conflict_free" not in name
        for name in result.undetected_checks
    ), "未检测项不得被命名成「无冲突」"


def test_没有条件时不标注条件冲突未检测项():
    result = evaluate_convergence(
        [StanceInput(user_id="alice", stance="conditional", confidence=0.9)]
    )
    assert result.undetected_checks == ()


def test_两类未检测项共存时顺序稳定且先C1后C2():
    result = evaluate_convergence(
        [
            StanceInput(
                user_id="bob",
                stance="conditional",
                confidence=0.9,
                non_negotiables=("预算不得超过 100 万",),
                conditions=("预算不超过 100 万",),
            )
        ]
    )
    assert result.undetected_checks == (
        UNDETECTED_NON_NEGOTIABLES,
        UNDETECTED_CONDITIONAL_CONFLICT,
    )


def test_反对置信度恰等阈值按硬阻断处理():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="oppose", confidence=0.7),
        ]
    )
    assert result.converged is False
    assert any("bob" in b and ">= 0.7" in b for b in result.blocking), (
        "0.7 是闭区间下界，恰等阈值即视为明确反对"
    )
    assert not any("< 0.7" in b for b in result.blocking)


def test_反对置信度略低于阈值按弱反对处理():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="oppose", confidence=0.699),
        ]
    )
    assert result.converged is False
    assert any("bob" in b and "< 0.7" in b for b in result.blocking)
    assert not any(">= 0.7" in b for b in result.blocking)


def test_同轮强反对与弱反对的阻塞点同时出现():
    result = evaluate_convergence(
        [
            StanceInput(user_id="alice", stance="support", confidence=0.9),
            StanceInput(user_id="bob", stance="oppose", confidence=0.8),
            StanceInput(user_id="carol", stance="oppose", confidence=0.3),
        ]
    )
    assert result.converged is False
    assert any("bob" in b and ">= 0.7" in b for b in result.blocking)
    assert any("carol" in b and "< 0.7" in b for b in result.blocking)
