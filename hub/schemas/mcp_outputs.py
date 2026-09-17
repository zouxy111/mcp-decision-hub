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
    v4 迁移列上。irreversible=true 时 irreversible_reason 必填
    （裁决 5a，2026-09-14；裁定 2，2026-09-17：校验在全通道生效）。"""

    title: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1)
    background: str = ""
    participant_ids: list[int] = Field(min_length=2, max_length=5)
    irreversible: bool = False
    irreversible_reason: str | None = None
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


class AskParticipantIn(_Strict):
    """ask_participant 入参（r5Am9i 第 5 个工具 / rpQt6D `POST /items/{id}/ask`）。

    **刻意不含任何配额字段**：owner 2026-09-14 裁决「ask 先不限制」，
    roR8Pk 第 4 条以「owner 决定不做」关闭。不要往这里加计数 / 滑窗 / 频率。
    """

    target_user_id: int
    question: str = Field(min_length=1, max_length=2000)
    round_number: int | None = Field(default=None, ge=1)


class AskParticipantOut(_Strict):
    """ask_participant 出参。重复提交同一问题返回首次那一行的同一个 id。"""

    question_id: str
    matter_id: str
    round_number: int
    target_user_id: int
    asked_by_user_id: int
    question: str
    created_at: str


class DirectedQuestionItem(_Strict):
    """挂到某人任务上下文里的「别人问我的问题」（待其提交立场时回答）。"""

    question_id: str
    round_number: int
    question: str
    asked_by_user_id: int


class DigestCurrentRound(_Strict):
    """digest 的「当前轮次」一节（PRD-03）。"""

    round_number: int
    status: str


class DigestDecision(_Strict):
    """digest 的「最新结论 / 决议草案」一节（PRD-03）。

    `final=False` 表示还只是草案（`pending_review`），此时 `text` 给
    `recommendation`（草案摘要）。**不含** `user_id` / `confidence` /
    逐人立场 —— 与 PRD-03 的「边界（不做）」逐条对齐。
    """

    decision_id: str
    status: str
    final: bool
    version: int
    cited_rounds: list
    text: str


class DigestOut(_Strict):
    """`get_digest` 出参 = **一页纸现状**（裁决 3，owner 2026-09-14 定，
    2026-09-16 复核「**不变**」；开工包 PRD-03 给出三个节名）：

    - `current_round`：当前轮次号与状态
    - `decision`：最新决议（已定稿）或决议草案摘要，**标注状态**；无则 None
    - `open_items`：未决开口清单

    另保留既有的摘要五字段与事项状态（实现先有、无害，且是「一页纸」的补充）。
    """

    matter_id: str
    status: str
    current_round: DigestCurrentRound
    decision: DigestDecision | None
    open_items: list[str]
    consensus_points: list
    divergences: list
    blind_spots: list
    open_questions: list
    convergence: str | None


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
    # ask_participant 的投递口：别人对本任务归属人的定向提问（当前轮）。
    # 默认空列表 —— _contract 用 exclude_defaults，所以「没有提问」时这个键
    # 根本不出现在出参里，既有消费端的字段集合不变。
    directed_questions: list[DirectedQuestionItem] = []


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
