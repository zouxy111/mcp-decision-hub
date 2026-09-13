"""立场层 Schema（Pydantic v2）。

字段定义以 PRD「Stance 字段」为单一事实源。入参 StanceCreate 与出参
StanceRead 分开：出参数组/派生字段（stance_id、matter_id、user_id、
created_at）不接受客户端注入。
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

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
    authority: str | None = Field(default=None, max_length=255)
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
    authority: str | None
    ttl_seconds: int | None
    urgency: Urgency
    visibility: Visibility
    content_hash: str
    created_at: datetime


class StanceAnalysisOut(BaseModel):
    """GET /api/items/{id}/stances/analysis 的响应契约（B1 收口）。

    与生产装配出口 `RoundStanceAnalysis`（hub/api/stances.py 的冻结
    dataclass）逐字段对齐：dataclass 是领域侧事实源，本模型只把它
    声明进 OpenAPI，让键集合成为 API 契约。tuple 字段经 JSON 序列化
    后即 list。"""

    model_config = ConfigDict(extra="forbid")

    fact_version: str
    sections: dict[str, str]
    violations: list[str]
    limitations: list[str]
    converged: bool
    degrading: bool
    undetected_checks: list[str]
    skipped_user_ids: list[str]
