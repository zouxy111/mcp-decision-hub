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
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    overall_deadline: Mapped[datetime | None] = mapped_column(nullable=True)
    item_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class MatterParticipant(Base):
    __tablename__ = "matter_participants"

    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    # 中转站 v1（Q1/Q2：agent_authority 挂「事项 × 参与人」，两档），v5 迁移同步
    agent_authority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visibility_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)


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
