from hub.domain.convergence_eval import StanceInput, evaluate_convergence


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
