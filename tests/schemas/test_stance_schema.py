"""立场 Schema 层测试（Pydantic v2 校验）。

入参模型 StanceCreate 是「单一事实源」字段定义的校验闸门：
多余字段禁止（防 mass assignment），枚举/边界必须在触库前拦下。
"""

import pytest
from pydantic import ValidationError

from hub.schemas.stance import StanceCreate


def _payload(**overrides) -> dict:
    data = {
        "round_number": 1,
        "stance": "support",
        "confidence": 0.5,
        "position_summary": "支持",
        "rationale_summary": "理由",
        "non_negotiables": ["底线"],
        "conditions": ["条件"],
        "open_questions": ["问题"],
        "depends_on": ["依赖"],
        "questions_for": [{"participant_id": "u-2", "question": "你的看法？"}],
        "disagreement_kind": "fact",
        "supersedes": None,
        "acting_as": "human",
        "authority": "can_commit",
        "ttl_seconds": 600,
        "urgency": "normal",
        "visibility": "participants",
        "content_hash": "b" * 64,
    }
    data.update(overrides)
    return data


def test_合法载荷可构造且覆盖全部枚举取值():
    for value in ("support", "oppose", "conditional", "abstain", "need_info"):
        assert StanceCreate(**_payload(stance=value)).stance == value
    for value in ("goal", "fact", "risk_appetite", "resource"):
        assert StanceCreate(**_payload(disagreement_kind=value)).disagreement_kind == value
    for value in ("human", "agent_on_behalf"):
        assert StanceCreate(**_payload(acting_as=value)).acting_as == value
    for value in ("low", "normal", "high"):
        assert StanceCreate(**_payload(urgency=value)).urgency == value
    for value in ("participants", "all"):
        assert StanceCreate(**_payload(visibility=value)).visibility == value


def test_非法立场枚举在构造期被拒且不触库():
    # 纯 Schema 测试：不注入 db_session，构造即失败，因此不存在任何落库动作。
    with pytest.raises(ValidationError) as exc:
        StanceCreate(**_payload(stance="banana"))
    assert exc.value.errors()[0]["loc"] == ("stance",)
