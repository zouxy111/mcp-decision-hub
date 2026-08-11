from hub.domain.credits import (
    CREDIT_GRANT_PER_CONTINUE,
    auto_round_limit,
    can_auto_advance,
)


def test_auto_limit_is_max_rounds_plus_granted():
    assert auto_round_limit(10, 0) == 10
    assert auto_round_limit(10, 2) == 12


def test_below_limit_can_advance():
    assert can_auto_advance(
        current_round_number=9, max_rounds=10, granted_extra_rounds=0
    ) is True


def test_at_limit_cannot_advance():
    assert can_auto_advance(
        current_round_number=10, max_rounds=10, granted_extra_rounds=0
    ) is False


def test_granted_extra_allows_one_more_round():
    assert can_auto_advance(
        current_round_number=10, max_rounds=10, granted_extra_rounds=1
    ) is True
    # 用掉这 1 轮额度后再次到达上限
    assert can_auto_advance(
        current_round_number=11, max_rounds=10, granted_extra_rounds=1
    ) is False


def test_grant_is_exactly_one_round():
    assert CREDIT_GRANT_PER_CONTINUE == 1
