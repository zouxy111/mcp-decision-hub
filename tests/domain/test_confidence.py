"""confidence 外发分档（PRD-07 · roR8pK 第 2 条）。

口径：档位是**显示粒度**，与收敛判定的 0.7 硬阻断**无对应关系**。
本文件同时钉住「边界不落在 0.7」这件事——它是防反推设计的一部分。
"""

import pytest

from hub.domain.confidence import HIGH, LOW, MEDIUM, confidence_band


@pytest.mark.parametrize("value,expected", [
    (0.0, LOW), (0.39, LOW),
    (0.4, MEDIUM), (0.6, MEDIUM), (0.7, MEDIUM), (0.79, MEDIUM),
    (0.8, HIGH), (0.95, HIGH), (1.0, HIGH),
])
def test_边界逐点(value, expected):
    assert confidence_band(value) == expected


def test_阈值0点7不落档位边界():
    """0.7 是收敛判定的硬阻断值，档位边界刻意避开它 —— 否则档位本身泄露阈值。

    0.7 必须与 0.6 / 0.69 同档（medium），不能单独成为一个边界。
    """
    assert confidence_band(0.69) == confidence_band(0.7) == confidence_band(0.71)


def test_NaN不静默落到low():
    """NaN 比任何值都小，若直接比大小会静默分到 low —— 那是静默错分。"""
    with pytest.raises(ValueError):
        confidence_band(float("nan"))


def test_档位只有三个取值():
    seen = {confidence_band(i / 100) for i in range(101)}
    assert seen == {LOW, MEDIUM, HIGH}
