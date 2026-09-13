"""立场层 Schema（Pydantic v2）。

字段定义以 PRD「Stance 字段」为单一事实源。入参 StanceCreate 与出参
StanceRead 分开：出参数组/派生字段（stance_id、matter_id、user_id、
created_at）不接受客户端注入。
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class StanceKind(StrEnum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    CONDITIONAL = "conditional"
    ABSTAIN = "abstain"
    NEED_INFO = "need_info"


class DisagreementKind(StrEnum):
    GOAL = "goal"
    FACT = "fact"
    RISK_APPETITE = "risk_appetite"
    RESOURCE = "resource"


class ActingAs(StrEnum):
    HUMAN = "human"
    AGENT_ON_BEHALF = "agent_on_behalf"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Visibility(StrEnum):
    PARTICIPANTS = "participants"
    ALL = "all"


class QuestionFor(BaseModel):
    """向某位参与人提出的问题。"""

    model_config = ConfigDict(extra="forbid")

    participant_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1000)


class StanceCreate(BaseModel):
    """POST /api/items/{matter_id}/stances 请求体。

    matter_id 来自路径、user_id 来自 Bearer 身份，均不在请求体中。
    """

    model_config = ConfigDict(extra="forbid")

    round_number: int = Field(ge=1)
    stance: StanceKind
    confidence: float = Field(ge=0.0, le=1.0)
    position_summary: str = Field(min_length=1, max_length=2000)
    rationale_summary: str = Field(min_length=1, max_length=5000)
    non_negotiables: list[ShortText] = Field(default_factory=list, max_length=50)
    conditions: list[ShortText] = Field(default_factory=list, max_length=50)
    open_questions: list[ShortText] = Field(default_factory=list, max_length=50)
    depends_on: list[ShortText] = Field(default_factory=list, max_length=50)
    questions_for: list[QuestionFor] = Field(default_factory=list, max_length=50)
    disagreement_kind: DisagreementKind | None = None
    supersedes: str | None = Field(default=None, max_length=48)
    acting_as: ActingAs
    # v3 起收敛为两档枚举（补充决策 S3）：authority 答「凭什么能提交」
    authority: Literal["propose_only", "can_commit"] | None = None
    ttl_seconds: int | None = Field(default=None, ge=60)
    urgency: Urgency = Urgency.NORMAL
    visibility: Visibility = Visibility.PARTICIPANTS
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class StanceRead(BaseModel):
    """立场出参：服务端补充 stance_id / matter_id / user_id / created_at。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    stance_id: str
    matter_id: str
    round_number: int
    user_id: int
    stance: StanceKind
    confidence: float
    position_summary: str
    rationale_summary: str
    non_negotiables: list[str]
    conditions: list[str]
    open_questions: list[str]
    depends_on: list[str]
    questions_for: list[QuestionFor]
    disagreement_kind: DisagreementKind | None
    supersedes: str | None
    acting_as: ActingAs
    authority: Literal["propose_only", "can_commit"] | None
    ttl_seconds: int | None
    urgency: Urgency
    visibility: Visibility
    content_hash: str
    created_at: datetime


# 与 hub/domain/audience.py 的占位文案保持一致（domain 层是纯函数、零依赖，
# 不反向导入 schema，故此处复制字面量并在测试中钉住行为）。
NO_DECISION_PLACEHOLDER = "这事目前还没定下来。"


class StanceAnalysisRead(BaseModel):
    """GET /api/items/{matter_id}/stances/analysis 出参（rsEXuh：禁止裸 dict）。

    faithfulness 校验（生成后）：sections 里凡承载事实的字段必须逐字来自
    事实基座，唯一例外是固定占位文案。此处校验其结构性推论（无需源事实
    即可判定）：「结论」为无结论占位文案时，不得同时出现「为什么这么定」/
    「为什么这次没有采纳」—— 没下结论却解释采纳理由，即不忠实。
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    fact_version: str
    sections: dict[str, str]
    violations: list[str]
    limitations: list[str]
    converged: bool
    degrading: bool
    undetected_checks: list[str]
    skipped_user_ids: list[str]

    @model_validator(mode="after")
    def _conclusion_is_faithful(self) -> "StanceAnalysisRead":
        conclusion = self.sections.get("结论")
        if conclusion == NO_DECISION_PLACEHOLDER:
            contradictory = {"为什么这么定", "为什么这次没有采纳"} & set(
                self.sections
            )
            if contradictory:
                raise ValueError(
                    f"无结论占位与采纳解释并存，不忠实于事实基座: {contradictory}"
                )
        return self


class StanceListItem(BaseModel):
    """立场列表出参：StanceRead 去掉私有字段后的公开子集（B27）。

    私有字段默认清单【待 owner 确认】：confidence / rationale_summary /
    non_negotiables / conditions / disagreement_kind —— 内部把握度与依据、
    底线与条件属「内部」信息，不进列表；单读端点（StanceRead）不受影响。
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    stance_id: str
    matter_id: str
    round_number: int
    user_id: int
    stance: StanceKind
    position_summary: str
    open_questions: list[str]
    depends_on: list[str]
    questions_for: list[QuestionFor]
    supersedes: str | None
    acting_as: ActingAs
    authority: Literal["propose_only", "can_commit"] | None
    ttl_seconds: int | None
    urgency: Urgency
    visibility: Visibility
    content_hash: str
    created_at: datetime
