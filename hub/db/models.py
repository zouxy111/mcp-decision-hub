"""SQLAlchemy models. Timestamps are naive UTC (see hub.domain.timeutil)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from hub.db.base import Base
from hub.domain.timeutil import utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    invitation_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invitation_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("tok"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Matter(Base):
    __tablename__ = "matters"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("mat"))
    initiator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    background: Mapped[str] = mapped_column(Text, default="", nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=72 * 3600, nullable=False)
    max_rounds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    initiator_participates: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    draft_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    granted_extra_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 中转站 v1（rpQt6D 载体映射，v4 迁移同步）：items ← Matter 扩展 4 列
    irreversible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 裁决 5a（2026-09-14）：irreversible 变更理由必填，随事项留痕
    irreversible_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    overall_deadline: Mapped[datetime | None] = mapped_column(nullable=True)
    item_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 协作模式扩展字段（Phase 2: 邀请链接与自动注册）
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True, default="project")
    # meeting: 会议模式（实时同步，立即处理）
    # project: 项目模式（异步协作，批处理）
    auto_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # TRUE: 所有参与者加入后自动启动
    # FALSE: 需要发起人手动启动
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class MatterParticipant(Base):
    __tablename__ = "matter_participants"

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    # 中转站 v1（Q1/Q2：agent_authority 挂「事项 × 参与人」，两档），v5 迁移同步
    agent_authority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visibility_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 留言板协作（v17 迁移同步）：受邀人注册时填的「我叫什么 / 我负责什么」。
    # 存「事项 × 参与人」而不是 users 表 —— 同一个人在 A 项目负责架构、
    # 在 B 项目只做评审，自我介绍是事项内的角色，不是账号属性。
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    responsibility: Mapped[str | None] = mapped_column(Text, nullable=True)


class MatterMessage(Base):
    """留言板协作（v17 建表 / v18 扩展）：一条发言 = 一次协作输入。

    留言板取代了「轮次 + 任务下发」：参与人不再等平台派题，随时留言即可，
    所有人互相可见，也不需要「提交/超时/收敛」这些状态机。

    v18（2026-10-05）按甲方要求扩了三类能力：
    * **上传前必须本人同意**：``human_approved_at`` 留痕；agent 代发时没这个
      时间戳就不许落库（服务层拒绝），板上会显示「本人已确认」。
    * **可以传 md 文件**：``attachment_name`` + ``attachment_md``（正文存库，
      云端直接可读，不落磁盘文件）。
    * **云端提问 → 本地处理 → 传回回答**：``kind="question"`` 带
      ``asked_to_user_id``；回答时带 ``reply_to_message_id``，原提问置为 answered。
    """

    __tablename__ = "matter_messages"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("msg"))
    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # message：普通发言；decision：发起人拍板的结论（置顶显示）
    # question：向某人定向提问（云端下发）；answer：对某条提问的回答
    kind: Mapped[str] = mapped_column(String(16), default="message", nullable=False)
    # 发言身份：human 本人发言 / agent_on_behalf 由本人 Agent 代发
    acting_as: Mapped[str] = mapped_column(String(32), default="human", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    # --- v18 ---
    # 本人明确同意上传的时间（human=本人直接发时自动等于 created_at）
    human_approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # md 附件：文件名 + 全文（存库，云端可读；不写磁盘）
    attachment_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 定向提问：问给谁 / 回答哪条 / 提问状态（open → answered）
    asked_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    reply_to_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("matter_messages.id"), nullable=True
    )
    question_status: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BoardSummary(Base):
    """留言板的**增量滚动总结**（v19，2026-10-05）。

    为什么需要：一块板最多 1000 条留言，agent 每次想知道"现在什么情况"都把
    全板拉下来既慢又吃上下文。于是云端每次有新留言就更新一次总结，agent
    先读总结（几百字），需要原文再拉具体那几条。

    增量口径：``covered_messages`` 记录「上次总结已经算进去多少条」。更新时
    只把**上一次总结**（很小）和**新增的那几条留言**（delta）喂给模型，
    不重发全板 —— 这是"保证速度、不占太多上下文"的实现关键。

    v20（2026-10-05）再加两件事：

    * ``document_version`` —— 每完成一轮总结就落一份 md 文档
      （:class:`BoardSummaryDocument`），这里记最新一版的版本号；
    * ``reread_requested*`` —— 「有人提了需求」的开关：设上之后下一轮总结
      从第一条留言重新读一遍（不是增量），理由和提出人一并留痕。

    一行 = 一块板（``matter_id`` 主键）。
    """

    __tablename__ = "board_summaries"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.id"), primary_key=True
    )
    # 当前进展：几段话说清"大家都在说什么、到哪一步了"
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 对当前任务的大概判断（还不能叫结论）
    judgement: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 已经明确的事实/共识 / 还没解决的问题
    key_points: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    open_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 已纳入总结的留言条数（增量游标）
    covered_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # idle / ok / failed
    generation_status: Mapped[str] = mapped_column(
        String(16), default="idle", nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- v20 ---
    # 最新一份总结文档的版本号（0 = 还没有文档）
    document_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 有人提了需求：下一轮总结重读全板（0/1）
    reread_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 提需求的时间和是谁提的（留痕，文档里会写明）
    reread_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reread_requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    reread_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class BoardSummaryDocument(Base):
    """云端总结文档（v20，2026-10-05）。

    甲方要求：「每轮大模型总结完云端信息，都写一个总结文档。下一次总结只读
    最新的留言板（除非有人提了需求），否则只改动总结后的内容。」

    落实成：**每完成一轮总结就追加一份不可修改的 md 文档**，``version`` 递增。
    文档里把「本轮之后新出现的留言」单独列一节 —— 这正是「只改动总结后的
    内容」的那部分；前面的进展 / 判断 / 已明确 / 待解决几节是从上一版延续
    下来的存量结论（模型会保留仍然成立的部分）。

    这份表只追加、不覆盖：想回看「上周这时候大家怎么看」直接翻旧版本。
    ``trigger``：

    * ``auto`` —— 有新留言，自动增量更新（常态）；
    * ``requested`` —— 有人提了需求，从第一条留言重读全板后重建；
    * ``imported`` —— 历史数据补齐（迁移/人工）。
    """

    __tablename__ = "board_summary_documents"
    __table_args__ = (UniqueConstraint("matter_id", "version"),)

    id: Mapped[str] = mapped_column(
        String(48), primary_key=True, default=lambda: new_id("bsd")
    )
    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.id"), nullable=False, index=True
    )
    # 这块板的第几份总结（从 1 开始，递增）
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 这份文档覆盖的留言区间（按发布时间排序的序号，1-based 闭区间）
    covered_from: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    covered_to: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 本轮新纳入总结的留言条数
    delta_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    judgement: Mapped[str] = mapped_column(Text, default="", nullable=False)
    key_points: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    open_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 完整文档正文（markdown）：可以直接下载 / 贴给别人看
    content_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # auto / requested / imported
    trigger: Mapped[str] = mapped_column(
        String(16), default="auto", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Round(Base):
    __tablename__ = "rounds"
    __table_args__ = (UniqueConstraint("matter_id", "round_number"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("rnd"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="generating", nullable=False)
    questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("tsk"))
    round_id: Mapped[str] = mapped_column(ForeignKey("rounds.id"), nullable=False)
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), nullable=False, index=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Output(Base):
    __tablename__ = "outputs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("out"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), unique=True, nullable=False)
    answers: Mapped[list] = mapped_column(JSON, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # v8 迁移同步（补充决策 S1）：代理提交路径的授权档位留痕，与 stances 对称
    authority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    # v7 迁移同步：主键从 task_id 作用域放开为 scope_type + scope_id，
    # 使幂等键可服务非 task 作用域（历史行迁移时 scope_type='task'、scope_id=task_id）。
    scope_type: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="task"
    )
    scope_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    matter_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class RoundSummary(Base):
    __tablename__ = "round_summaries"

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: new_id("sum"))
    round_id: Mapped[str] = mapped_column(ForeignKey("rounds.id"), unique=True,
                                          nullable=False)
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), nullable=False,
                                           index=True)
    consensus_points: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    divergences: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    blind_spots: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    open_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    convergence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    generation_status: Mapped[str] = mapped_column(String(16), default="ok",
                                                   nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v6 迁移同步：收敛评估的展示参考分与聚类结果（agreement_score 不作判据）
    agreement_score: Mapped[float | None] = mapped_column(nullable=True)
    clusters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Resolution(Base):
    __tablename__ = "resolutions"
    __table_args__ = (
        UniqueConstraint("matter_id", "version"),
        UniqueConstraint("source_round_id"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: new_id("res"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), nullable=False,
                                           index=True)
    source_round_id: Mapped[str] = mapped_column(ForeignKey("rounds.id"),
                                                 nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_review",
                                        nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    risks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    divergences: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    cited_rounds: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class ParticipantQuestion(Base):
    """定向提问（r5Am9i · ask_participant）：一行 = 「谁在第几轮问谁什么」。

    为什么单独建表、而不是写进 ``stances.questions_for``：那一列语义是
    **本人向他人提问**，且 ``stances`` 按 (matter_id, round_number, user_id)
    唯一 —— 目标本轮尚未提交立场时连行都不存在，无处可挂；强行写别人的行
    还会破坏 ``Stance.content_hash``。详见
    ``outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md``。

    ``question_hash`` 是正文的稳定摘要，承担幂等：同一 (事项, 轮次, 被问人,
    提问人, 问题正文) 只落一行，重发返回首次结果，不产生第二条。
    """

    __tablename__ = "participant_questions"
    __table_args__ = (
        UniqueConstraint("matter_id", "round_number", "target_user_id",
                         "asked_by_user_id", "question_hash"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: new_id("pqt"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"),
                                           nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                                nullable=False, index=True)
    asked_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                                  nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Stance(Base):
    """立场层：某参与人在某轮次对议题的结构化立场（新增表，不改既有表）。

    枚举列由 CHECK 约束兜底（stance / acting_as / urgency / visibility），
    取值与 hub.schemas.stance 的 StrEnum 保持一致。

    注意：迁移机制已存在（hub/db/migrations/，v1/v2 起）。CHECK 约束对
    **新建库**直接生效（create_all）；存量库走迁移步骤重建表补齐，不再
    需要手工迁移。
    """

    __tablename__ = "stances"
    __table_args__ = (
        UniqueConstraint("matter_id", "round_number", "user_id"),
        CheckConstraint(
            "stance IN ('support', 'oppose', 'conditional', 'abstain', 'need_info')",
            name="ck_stances_stance",
        ),
        CheckConstraint(
            "acting_as IN ('human', 'agent_on_behalf')",
            name="ck_stances_acting_as",
        ),
        CheckConstraint(
            "urgency IN ('low', 'normal', 'high')",
            name="ck_stances_urgency",
        ),
        CheckConstraint(
            "visibility IN ('participants', 'all')",
            name="ck_stances_visibility",
        ),
        CheckConstraint(
            "authority IS NULL OR authority IN ('propose_only', 'can_commit')",
            name="ck_stances_authority",
        ),
    )

    stance_id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                           default=lambda: new_id("stn"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), nullable=False,
                                           index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    stance: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    position_summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale_summary: Mapped[str] = mapped_column(Text, nullable=False)
    non_negotiables: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    conditions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    open_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    depends_on: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    questions_for: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    disagreement_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(ForeignKey("stances.stance_id"),
                                                   nullable=True)
    acting_as: Mapped[str] = mapped_column(String(32), nullable=False)
    # v3 迁移同步（补充决策 S3/S1）：authority 收敛为两档枚举（propose_only /
    # can_commit，NULL 允许）；存量自由文本授权原值只读保留于 authority_legacy，
    # 禁止启发式回填（PRD §3.4-1）。
    authority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    authority_legacy: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    urgency: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="participants",
                                            nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class InvitationLink(Base):
    """邀请链接表：用于自动注册和快速加入协作事项。
    
    每个链接对应一个参与者名额，消费后自动创建账号并加入事项。
    短链格式：6位字符（排除易混淆字符 0/O/I/l/1），如 xY9kL2
    
    状态流转：
    - active: 可用（未消费且未过期）
    - consumed: 已消费（成功创建账号）
    - expired: 已过期
    - revoked: 已撤销（发起人手动取消）
    """
    
    __tablename__ = "invitation_links"
    __table_args__ = (
        UniqueConstraint("matter_id", "short_code"),
    )
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True, 
                                    default=lambda: new_id("inv"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), 
                                          nullable=False, index=True)
    short_code: Mapped[str] = mapped_column(String(8), unique=True, 
                                           nullable=False, index=True)
    # 6位短链码，排除易混淆字符
    invited_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 可选：发起人指定的参与者姓名（便于识别）
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # active | consumed | expired | revoked
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 可重复使用次数（通常为1），None 表示无限制
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    # 有效期（默认3天）
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), 
                                                   nullable=True)


class InvitationConsumption(Base):
    """邀请链接消费记录：记录谁在何时通过哪个链接加入。
    
    用途：
    1. 审计追溯（谁通过哪个链接进来的）
    2. 防止重复消费（同一链接只能被同一人使用一次）
    3. IP 限流（防止批量注册攻击）
    """
    
    __tablename__ = "invitation_consumptions"
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True, 
                                    default=lambda: new_id("cons"))
    invitation_id: Mapped[str] = mapped_column(ForeignKey("invitation_links.id"), 
                                              nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), 
                                        nullable=False, index=True)
    # 自动创建的用户
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 记录 IP 用于限流和审计
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 记录 User-Agent
    consumed_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class BatchProcessingQueue(Base):
    """批处理队列：暂存待处理的立场提交，等待批量处理。
    
    工作流：
    1. 用户提交立场 → 插入队列（status=pending）
    2. 时间窗口到期 → 批量标记为 processing
    3. LLM 批量生成摘要 → 标记为 completed
    4. 失败的可以重试 → 标记为 failed
    """
    
    __tablename__ = "batch_processing_queue"
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True, 
                                    default=lambda: new_id("bpq"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), 
                                          nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stance_id: Mapped[str] = mapped_column(ForeignKey("stances.stance_id"), 
                                          nullable=False, unique=True)
    # 关联的立场记录
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # pending | processing | completed | failed
    batch_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    # 批次标识（同一批处理的记录共享）
    scheduled_at: Mapped[datetime] = mapped_column(nullable=False)
    # 预期处理时间（时间窗口结束时间）
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class LlmConfig(Base):
    """LLM 运行时配置（单行表，``id`` 恒为 1；无行 = 从未在页面上配置过）。

    为什么是「单行强类型表」而不是 KV：这些字段的取值集合是确定的、要被
    migration 的冻结 DDL 原样复现、要被测试逐列断言 —— 列出来比塞进
    ``key/value`` 更容易被读懂和被证伪。

    为什么 ``api_key`` 是明文：调用时必须用原文作为 ``Bearer`` 发出，可逆
    存储是客观需要。它与既有的 ``.env`` 里的 ``DEEPSEEK_API_KEY`` 同机同目录，
    属同一暴露等级，本表没有引入新的暴露面。补偿措施有三条：页面只回显掩码
    （:func:`hub.llm.runtime.mask_secret`），审计事件只记「有没有改」不落原文，
    日志纪律（PRD 10.1）本来就不记 key。

    生效语义见 :mod:`hub.llm.runtime`：DB 有值以 DB 为准，否则回落 env。
    """

    __tablename__ = "llm_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_llm_config_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow,
                                                 nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True)


class Meeting(Base):
    """会议实例（voice-copilot 实时协作模式）。
    
    关联到 Matter，每个 Matter 可以有多个会议（多轮讨论）。
    会议模式特点：
    - 实时：参与人说话后立即上传立场（本地 LLM 整理后的文本）
    - 异步：3 分钟超时窗口，无需所有人同时在线
    - 收敛：云端 LLM 分析共识/分歧，返回精简摘要
    """
    __tablename__ = "meetings"
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                   default=lambda: new_id("mtg"))
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"),
                                          nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # active: 进行中  |  completed: 已结束
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # 每轮超时时间（分钟），从首次提交开始计时
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 首次收到立场的时间
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class MeetingStance(Base):
    """会议立场（voice-copilot 提交的本地 LLM 整理结果）。
    
    只存储文本，不存储原始转写和检索结果（省 token）。
    本地 LLM 整理时已经结合了本地知识库，这里只需要整理后的立场文本。
    """
    __tablename__ = "meeting_stances"
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                   default=lambda: new_id("mst"))
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"),
                                           nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"),
                                        nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 本地 LLM 整理后的立场文本
    submitted_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    
    __table_args__ = (
        # 同一会议、同一轮次、同一用户只能提交一次
        UniqueConstraint("meeting_id", "round_number", "user_id",
                        name="uq_meeting_stance_per_round"),
    )


class MeetingConvergence(Base):
    """会议收敛结果（云端 LLM 生成的摘要）。
    
    精简 JSON 格式，只包含：
    - consensus: 共识点数组
    - divergences: 分歧点数组  
    - follow_ups: 追问数组
    
    每轮最多 3 条，节省 token。
    """
    __tablename__ = "meeting_convergences"
    
    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                   default=lambda: new_id("mcv"))
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"),
                                           nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    consensus: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 共识点：["共识1", "共识2", ...]
    divergences: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 分歧点：["分歧1", "分歧2", ...]
    follow_ups: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 追问：["问题1", "问题2", ...]
    generated_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    
    __table_args__ = (
        # 每个会议每轮只能有一个收敛结果
        UniqueConstraint("meeting_id", "round_number",
                        name="uq_meeting_convergence_per_round"),
    )
