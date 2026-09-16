"""四个 MCP 工具返回值的 Pydantic 输出契约（D1）。

形状在此唯一定义：`hub/mcp_server/methods.py` 的返回值先经对应模型校验
再交还调用方，键名/键集合漂移（改字段名、丢字段、多字段）在产出时就失败，
而不是在消费端静默变形。字段名与既有 JSON 键逐一对齐——调用方（agent 端）
依赖这些键，禁止在本模块擅改。

`extra="forbid"`：多出来的字段视为契约破坏（宁可产出时报错，不带病出门）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeclareItemIn(_Strict):
    """declare_item 入参（rpQt6D：items 载体 = Matter 扩展 4 列）。

    participant_ids 为 2–5 名参与人（FR-05）；question 复用 Matter.goal；
    irreversible / options / overall_deadline / item_version 落在 A1 的
    v4 迁移列上。"""

    title: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1)
    background: str = ""
    participant_ids: list[int] = Field(min_length=2, max_length=5)
    irreversible: bool = False
    options: list[str] | None = None
    overall_deadline: str | None = None


class DecideItemIn(_Strict):
    """decide_item 入参（rpQt6D：`POST /items/{id}/decide`）。

    与 ``decide_resolution`` 的守卫分工：本模型只管**形状与枚举**——
    非法 decision 值在这里就 422，永远到不了服务层；「仅发起人可拍板」
    「irreversible 拒绝」「非 awaiting_decision 拒绝」属状态判定，归
    ``hub/api/resolutions.py``。

    decision 取 ``hub/domain/resolution.py`` 的 DECISIONS 字面值；
    expected_version 是 version+status 乐观锁的显式期望值（FR-21b），
    调用方须回传它看到的版本号，冲突返回 RESOLUTION_VERSION_CONFLICT。
    """

    decision: Literal["approved", "modified", "rejected"]
    expected_version: int = Field(ge=1)
    final_text: str | None = None
    rationale: str | None = None


class DeclareItemOut(_Strict):
    matter_id: str
    status: str
    item_version: int
    irreversible: bool


class ItemBrief(_Strict):
    """列表项（rpQt6D：`GET /items?participant=me&state=open`）。

    只给「用来认领/挑选哪一条」的最小字段，不塞正文——正文读
    `GET /items/{id}/digest`。`item_version` / `irreversible` 在库里是
    nullable（v4 迁移前建的行），对外统一收敛为与 `DeclareItemOut` 同型。
    """

    matter_id: str
    title: str
    status: str
    item_version: int
    irreversible: bool
    overall_deadline: str | None


class ItemListOut(_Strict):
    items: list[ItemBrief]


class RoundSummaryOut(_Strict):
    """get_summary 出参：最新一轮 ok 摘要（五字段无身份，PRD 9.2）。"""

    round_id: str
    round_number: int
    consensus_points: list
    divergences: list
    blind_spots: list
    open_questions: list
    convergence: str | None


class SummaryPayload(_Strict):
    """一轮 ok 摘要的正文五元组（PRD 9.2 / FR-07）。"""

    consensus_points: list
    divergences: list
    blind_spots: list
    open_questions: list
    convergence: str | None  # 摘要生成时可能尚无收敛结论


class PreviousSummaryView(SummaryPayload):
    """get_task 的 previous_summary：上一轮 ok 摘要；无则 None，绝不伪造。"""

    round_number: int


class RoundSummaryItem(SummaryPayload):
    """get_matter_status 里某轮的真实 ok 摘要（至多一条，唯一约束）。"""

    summary_id: str
    created_at: str


class ResolutionView(_Strict):
    """决议的阶段与版本视图；draft/final 正文只留在 web 决策页。"""

    resolution_id: str
    status: str
    version: int
    cited_rounds: list
    created_at: str
    decided_at: str | None  # pending_review 时为 None，键仍在


class RoundStatusView(_Strict):
    round_id: str
    round_number: int
    status: str
    tasks_total: int
    tasks_submitted: int
    summaries: list[RoundSummaryItem]


class ParticipantProgressItem(_Strict):
    user_id: int
    username: str
    task_id: str
    task_status: str


class PendingTaskItem(_Strict):
    task_id: str
    matter_id: str
    matter_title: str
    round_number: int
    deadline_at: str | None
    created_at: str


class PendingTasksOut(_Strict):
    tasks: list[PendingTaskItem]
    next_cursor: str | None  # 无下一页时键仍在、值为 None
    next_poll_after: str


class MatterBrief(_Strict):
    matter_id: str
    title: str
    background: str
    goal: str


class RoundBrief(_Strict):
    round_id: str
    round_number: int
    questions: list


class TaskDetailOut(_Strict):
    task_id: str
    status: str
    matter: MatterBrief
    round: RoundBrief
    previous_summary: PreviousSummaryView | None
    deadline_at: str | None
    llm_provider: str


class SubmitOutputOut(_Strict):
    task_id: str
    status: str
    submitted_at: str
    output_id: str


class MatterStatusOut(_Strict):
    """participant_progress 仅发起人可见：有默认值 None + exclude_defaults，
    参与人视角下该键不存在（与既有行为逐键一致）。其余字段全部必填，
    值为 None 的键（如未出决议时的 resolution）保留。"""

    matter_id: str
    title: str
    status: str
    rounds_total: int
    recent_rounds: list[RoundStatusView]
    resolution: ResolutionView | None
    participant_progress: list[ParticipantProgressItem] | None = None
