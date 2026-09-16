"""confidence 外发分档（PRD-07 · roR8Pk 第 2 条）。

**为什么需要它**：`confidence` 原值（float）配合公开的「0.7 硬阻断」语义
（`hub/domain/convergence_eval.py`），可以反推「谁在卡结论」。外发粒度必须
从原值收窄为分档。

⚠️ **档位是显示粒度，与收敛判定的 0.7 硬阻断没有任何对应关系** —— 边界刻意
不落在 0.7 上（0.4 / 0.8），避免档位本身泄露阈值位置。后人不要「对齐」成 0.7。

**边界（PRD-07 明写）**：不改收敛判定逻辑、不改数据库列、不动列表端点
（`StanceListItem` 早已裁掉该字段）。本模块只服务 `StanceRead` 的外发。
"""

from __future__ import annotations

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

BANDS = (LOW, MEDIUM, HIGH)


def confidence_band(confidence: float) -> str:
    """把原值分档：``[0, 0.4)`` low / ``[0.4, 0.8)`` medium / ``[0.8, 1.0]`` high。

    边界取左闭右开；`1.0` 落在 high。非法输入（越界）由调用方的入参校验拦，
    本函数只做纯映射，不再重复判 —— 但会拒绝 NaN（NaN 比任何值都小，会静默
    落到 low，属静默错分，宁可报错）。
    """
    if confidence != confidence:  # NaN
        raise ValueError("confidence 不能是 NaN")
    if confidence < 0.4:
        return LOW
    if confidence < 0.8:
        return MEDIUM
    return HIGH
