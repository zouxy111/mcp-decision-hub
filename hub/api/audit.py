"""Audit trail writer. detail must never contain secrets, tokens or output bodies."""

from sqlalchemy.orm import Session

from hub.db.models import AuditEvent

LOGIN_SUCCESS = "login_success"
LOGIN_FAILED = "login_failed"
LOGIN_RATE_LIMITED = "login_rate_limited"
PASSWORD_CHANGED = "password_changed"
INVITE_CREATED = "invite_created"
INVITE_CONSUMED = "invite_consumed"
INVITE_REVOKED = "invite_revoked"
TOKEN_ISSUED = "token_issued"
TOKEN_REVOKED = "token_revoked"
MATTER_CREATED = "matter_created"
MATTER_STARTED = "matter_started"
TASK_SUBMITTED = "task_submitted"
OUTPUT_REPLAYED = "output_replayed"
INVALID_STATE_TRANSITION = "invalid_state_transition"
FORBIDDEN_DENIED = "forbidden_denied"
ROUND_SUMMARIZED = "round_summarized"
CONVERGENCE_DECIDED = "convergence_decided"
ROUND_GENERATED = "round_generated"
MATTER_BLOCKED = "matter_blocked"
MATTER_CONTINUED = "matter_continued"
MATTER_CANCELLED = "matter_cancelled"
LLM_FAILED = "llm_failed"
RESOLUTION_DRAFTED = "resolution_drafted"
RESOLUTION_DECIDED = "resolution_decided"
MATTER_COMPLETED = "matter_completed"
MATTER_AWAITING_DECISION = "matter_awaiting_decision"
TASK_TIMEOUT = "task_timeout"
TASK_REASSIGNED = "task_reassigned"
STANCE_READ = "stance_read"
# 模型配置变更（/admin/model 保存）。detail = {model, base_url, changed} ——
# **不含 api_key 原文**，只记「有没有改到它」（changed 里出现 "api_key"）。
# 这条事件的存在理由是：一次改配置会改变后续所有 LLM 调用的模型与账单归属，
# 却没有别的痕迹。
LLM_CONFIG_UPDATED = "llm_config_updated"
# 契约（2026-09-12 冻结）：收敛判定因脏数据降级时写入，detail 带 {stance, user_id}。
CONVERGENCE_DEGRADED = "convergence_degraded"
# 补充决策 A（owner 已批）：迁移步骤应用时写入，detail = {version, name}，
# actor_user_id/matter_id 均为 NULL（系统事件）。写入守卫在迁移层。
SCHEMA_MIGRATED = "schema_migrated"
# owner 2026-09-14 批准：分析视图分发留痕，detail 带 {viewer_user_id,
# fact_version, content_hash}（不含立场正文）。
AUDIENCE_VIEW_DELIVERED = "audience_view_delivered"
# owner 2026-09-14 批准：过期立场被排除时写入，detail 带 {matter_id,
# round_number, user_id, ttl_seconds}。
STANCE_EXPIRED = "stance_expired"
# 待办事项事件（v21，2026-10-07）。与决议事件分开记：待办是执行跟踪，
# 决议是决策留痕，两者的审计查询场景不同，合在一起会互相淹没。
TODO_CREATED = "todo_created"
TODO_UPDATED = "todo_updated"
# AI 抽取出的待办落了库（等人工确认）。detail 带 todo_id 与来源消息。
TODO_EXTRACTED = "todo_extracted"
# 任务卡事件（v22，2026-10-10）。验收标准从讨论到发布、交付、验收全留痕。
TASK_CARD_CREATED = "task_card_created"
TASK_CARD_UPDATED = "task_card_updated"
TASK_CARD_PUBLISHED = "task_card_published"
TASK_CARD_CLOSED = "task_card_closed"
TASK_DELIVERY_SUBMITTED = "task_delivery_submitted"
TASK_DELIVERY_DECIDED = "task_delivery_decided"

# Max chars of a decision rationale stored in audit detail (design decision 12).
AUDIT_RATIONALE_MAX = 500


def record_audit(
    session: Session,
    event_type: str,
    *,
    actor_user_id: int | None = None,
    matter_id: str | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_type=event_type,
            matter_id=matter_id,
            detail=detail,
        )
    )
    session.flush()
