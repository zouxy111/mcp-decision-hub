"""Business implementation of the MCP methods: the 4 M1 agent tools plus the
r5Am9i stance-layer tools (declare_item / submit_stance / read_stance /
get_summary; ask_participant / decide_item / get_digest pending product
decisions — see the handover PRD)."""

import base64
import json
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api import matters as matters_svc
from hub.api import resolutions as resolutions_svc
from hub.api import stances as stance_svc
from hub.api.errors import ApiError
from hub.api.matters import is_participant
from hub.api.passwords import sha256_hex
from hub.config import Settings
from hub.db.models import (
    IdempotencyRecord,
    Matter,
    MatterParticipant,
    Output,
    ParticipantQuestion,
    Resolution,
    Round,
    RoundSummary,
    Task,
    User,
)
from hub.domain.approval import ApprovalWindowError, validate_approved_at
from hub.domain.digest import compute_content_digest
from hub.domain.idempotency import IdempotencyDecision, decide_idempotency
from hub.domain.limits import (
    ContentLimitError,
    validate_content_limits,
    validate_request_body_size,
)
from hub.domain.participants import ParticipantValidationError
from hub.domain.timeutil import iso_z, parse_iso_z, utcnow
from hub.schemas.mcp_outputs import (
    AskParticipantIn,
    AskParticipantOut,
    DecideItemIn,
    DeclareItemIn,
    DeclareItemOut,
    DigestOut,
    ItemListOut,
    MatterStatusOut,
    PendingTasksOut,
    ResolutionView,
    RoundSummaryOut,
    SubmitOutputOut,
    TaskDetailOut,
)
from hub.schemas.stance import StanceCreate, StanceListItem, StanceRead

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _contract(model_cls, data, *, mode: str = "python"):
    """返回值先过 Pydantic 输出契约再出门：键漂移/多字段/缺字段在这里炸，
    不带病交给调用方。模型字段与既有 JSON 键逐一对齐，只校验不改形。"""
    return model_cls.model_validate(data).model_dump(mode=mode,
                                                     exclude_defaults=True)


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ApiError(401, "AUTH_INVALID_TOKEN", "令牌对应用户不存在")
    return user


def _require_matter(session: Session, *, matter_id: str, user_id: int) -> Matter:
    """读侧成员闸门：事项成员（发起人 ∪ 参与人）放行，其余一律 404。

    ``matters.get_matter_for_user`` 对非成员返回 **None 而不是抛错**，所以
    调用方必须自己判。历史上有两处只调不判，闸门等于不存在——任何持令牌的
    用户都能读到别人事项的摘要与一页纸（2026-09-16 由
    ``tests/api/test_rpQt6D_items_endpoints.py`` 抓到）。新写的读侧方法一律
    走本函数，不要直接调 ``get_matter_for_user``。

    用 404 而非 403：避免泄露「该事项存在」，与 ``hub/api/stances.py`` 的
    ``_require_matter_access`` 同一口径、同一句文案。
    """
    matter = matters_svc.get_matter_for_user(session, matter_id=matter_id,
                                             user=_user(session, user_id))
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    return matter


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as e:
        raise ApiError(422, "CURSOR_INVALID", "cursor 无法解析") from e


def mcp_list_pending_tasks(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    effective_limit = DEFAULT_LIMIT if limit is None else min(max(1, limit), MAX_LIMIT)
    offset = _decode_cursor(cursor) if cursor else 0
    rows = session.execute(
        select(Task, Matter.title, Round.round_number)
        .join(Matter, Task.matter_id == Matter.id)
        .join(Round, Task.round_id == Round.id)
        .where(Task.assignee_id == user_id, Task.status == "pending")
        .order_by(Task.created_at, Task.id)
        .offset(offset)
        .limit(effective_limit + 1)
    ).all()
    has_more = len(rows) > effective_limit
    rows = rows[:effective_limit]
    poll_seconds = (settings.poll_seconds_active if rows
                    else settings.poll_seconds_idle)
    return _contract(PendingTasksOut, {
        "tasks": [
            {
                "task_id": task.id,
                "matter_id": task.matter_id,
                "matter_title": title,
                "round_number": round_number,
                "deadline_at": iso_z(task.deadline_at) if task.deadline_at else None,
                "created_at": iso_z(task.created_at),
            }
            for task, title, round_number in rows
        ],
        "next_cursor": _encode_cursor(offset + effective_limit) if has_more else None,
        "next_poll_after": iso_z(utcnow() + timedelta(seconds=poll_seconds)),
    })


def mcp_get_task(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    task_id: str,
) -> dict:
    task = session.get(Task, task_id)
    if task is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    if task.assignee_id != user_id:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "get_task", "task_id": task_id},
        )
        raise ApiError(403, "FORBIDDEN_SCOPE", "无权访问该任务")
    matter = session.get(Matter, task.matter_id)
    rnd = session.get(Round, task.round_id)
    return _contract(TaskDetailOut, {
        "task_id": task.id,
        "status": task.status,
        "matter": {
            "matter_id": matter.id,
            "title": matter.title,
            "background": matter.background,
            "goal": matter.goal,
        },
        "round": {
            "round_id": rnd.id,
            "round_number": rnd.round_number,
            "questions": rnd.questions,
        },
        "previous_summary": _previous_summary_view(session, rnd),
        "deadline_at": iso_z(task.deadline_at) if task.deadline_at else None,
        "llm_provider": settings.llm_provider_name,
        "directed_questions": _directed_questions_view(
            session, matter_id=matter.id, target_user_id=task.assignee_id),
    })


def _limit_api_error(e: ContentLimitError) -> ApiError:
    return ApiError(
        422, "CONTENT_LIMIT_EXCEEDED", str(e),
        details={"limit": e.limit_name, "actual": e.actual, "max": e.limit},
    )


def _canonical_answers(answers: list[dict]) -> str:
    return json.dumps(
        sorted([[a["question_id"], a["content"]] for a in answers]),
        ensure_ascii=False,
    )


def _validate_answers_shape(answers, valid_question_ids: set[str]) -> None:
    if not isinstance(answers, list) or len(answers) == 0:
        raise ApiError(422, "QUESTION_INVALID", "answers 为空或缺失")
    seen: set[str] = set()
    for item in answers:
        if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
            raise ApiError(422, "QUESTION_INVALID", "answers 项缺少 question_id")
        if not isinstance(item.get("content"), str) or not item["content"]:
            raise ApiError(422, "QUESTION_INVALID", "answers 项 content 为空")
        qid = item["question_id"]
        if qid in seen:
            raise ApiError(422, "QUESTION_INVALID", f"重复题号: {qid}")
        if qid not in valid_question_ids:
            raise ApiError(422, "QUESTION_INVALID", f"未知或非本任务题号: {qid}")
        seen.add(qid)


def mcp_submit_output(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    payload: dict,
) -> dict:
    received_at = utcnow()
    try:
        validate_request_body_size(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            body_limit=settings.request_body_limit,
        )
    except ContentLimitError as e:
        raise _limit_api_error(e) from e

    task_id = payload.get("task_id")
    idempotency_key = payload.get("idempotency_key")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError(422, "VALIDATION_FAILED", "task_id 缺失或类型错误")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ApiError(422, "VALIDATION_FAILED", "idempotency_key 为必填")

    task = session.get(Task, task_id)
    if task is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务不存在")
    if task.assignee_id != user_id:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "submit_output", "task_id": task_id},
        )
        raise ApiError(403, "FORBIDDEN_SCOPE", "无权向该任务提交")

    rnd = session.get(Round, task.round_id)
    valid_qids = {q["question_id"] for q in rnd.questions}
    answers = payload.get("answers")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ApiError(422, "VALIDATION_FAILED", "notes 类型错误")
    _validate_answers_shape(answers, valid_qids)
    try:
        validate_content_limits(
            answers, notes,
            item_limit=settings.content_item_limit,
            total_limit=settings.content_total_limit,
            notes_limit=settings.notes_limit,
        )
    except ContentLimitError as e:
        raise _limit_api_error(e) from e

    if payload.get("human_approved") is not True:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "缺少 human_approved: true 人审声明")
    approved_at_raw = payload.get("approved_at")
    if not isinstance(approved_at_raw, str):
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED", "approved_at 缺失或类型错误")
    try:
        approved_at = parse_iso_z(approved_at_raw)
    except ValueError as e:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "approved_at 不是合法 ISO 8601 时间") from e
    try:
        validate_approved_at(approved_at, received_at)
    except ApprovalWindowError as e:
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED", str(e)) from e
    digest = payload.get("content_digest")
    if not isinstance(digest, str) or digest != compute_content_digest(answers, notes):
        raise ApiError(422, "HUMAN_APPROVAL_REQUIRED",
                       "content_digest 与提交正文不匹配（摘要不匹配）")

    fingerprint = sha256_hex(
        json.dumps(payload, sort_keys=True, ensure_ascii=False)
    )
    record = session.get(IdempotencyRecord, ("task", task_id, idempotency_key))
    existing_output = session.scalar(
        select(Output).where(Output.task_id == task_id)
    )
    decision = decide_idempotency(
        task_status=task.status,
        key_seen=record is not None,
        key_body_matches=(
            record.request_fingerprint == fingerprint if record else None
        ),
        content_matches=(
            (
                _canonical_answers(existing_output.answers)
                == _canonical_answers(answers)
                and (existing_output.notes or "") == (notes or "")
            )
            if existing_output
            else None
        ),
    )

    if decision is IdempotencyDecision.REPLAY_SAME_KEY:
        return _contract(SubmitOutputOut, json.loads(record.response_json))
    if decision is IdempotencyDecision.CONFLICT_SAME_KEY:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等键对应不同请求体")
    if decision is IdempotencyDecision.INVALID_STATE:
        audit.record_audit(
            session, audit.INVALID_STATE_TRANSITION, actor_user_id=user_id,
            matter_id=task.matter_id,
            detail={"action": "submit_output", "task_id": task_id,
                    "task_status": task.status},
        )
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       f"任务状态 {task.status} 不允许提交")
    if decision is IdempotencyDecision.REPLAY_EQUIVALENT:
        audit.record_audit(
            session, audit.OUTPUT_REPLAYED, actor_user_id=user_id,
            matter_id=task.matter_id, detail={"task_id": task_id},
        )
        return _contract(SubmitOutputOut, {
            "task_id": task_id,
            "status": "submitted",
            "submitted_at": iso_z(task.submitted_at),
            "output_id": existing_output.id,
        })
    if decision is IdempotencyDecision.ALREADY_SUBMITTED:
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在不同内容的提交")

    # CREATE_NEW: conditional UPDATE first so a lost race never overwrites.
    result = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "pending")
        .values(status="submitted", submitted_at=received_at)
    )
    if result.rowcount != 1:
        session.rollback()
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在其他提交（并发竞争）")
    output = Output(
        task_id=task_id, answers=answers, notes=notes,
        approved_at=approved_at, content_digest=digest,
    )
    session.add(output)
    session.flush()
    response = {
        "task_id": task_id,
        "status": "submitted",
        "submitted_at": iso_z(received_at),
        "output_id": output.id,
    }
    session.add(
        IdempotencyRecord(
            scope_type="task",
            scope_id=task_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response_json=json.dumps(response, ensure_ascii=False),
        )
    )
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise ApiError(409, "TASK_ALREADY_SUBMITTED",
                       "任务已存在其他提交（唯一约束）") from e
    audit.record_audit(
        session, audit.TASK_SUBMITTED, actor_user_id=user_id,
        matter_id=task.matter_id, detail={"task_id": task_id},
    )
    return _contract(SubmitOutputOut, response)


def mcp_get_matter_status(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    rounds_before: int | None = None,
) -> dict:
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在")
    involved = matter.initiator_id == user_id or is_participant(
        session, matter_id=matter_id, user_id=user_id
    )
    if not involved:
        audit.record_audit(
            session, audit.FORBIDDEN_DENIED, actor_user_id=user_id,
            matter_id=matter_id, detail={"action": "get_matter_status"},
        )
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在或不可见")
    all_rounds = session.scalars(
        select(Round)
        .where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc())
    ).all()
    visible = all_rounds
    if rounds_before is not None:
        visible = [r for r in visible if r.round_number < rounds_before]
    recent = visible[:3]
    round_views = []
    for rnd in recent:
        tasks = session.scalars(
            select(Task).where(Task.round_id == rnd.id)
        ).all()
        round_views.append({
            "round_id": rnd.id,
            "round_number": rnd.round_number,
            "status": rnd.status,
            "tasks_total": len(tasks),
            "tasks_submitted": sum(1 for t in tasks if t.status == "submitted"),
            "summaries": _round_summaries_view(session, rnd.id),
        })
    result = {
        "matter_id": matter.id,
        "title": matter.title,
        "status": matter.status,
        "rounds_total": len(all_rounds),
        "recent_rounds": round_views,
        "resolution": _resolution_view(session, matter.id),
    }
    if matter.initiator_id == user_id and round_views:
        latest = round_views[0]
        tasks = session.scalars(
            select(Task).where(Task.round_id == latest["round_id"])
        ).all()
        result["participant_progress"] = [
            {
                "user_id": t.assignee_id,
                "username": session.get(User, t.assignee_id).username,
                "task_id": t.id,
                "task_status": t.status,
            }
            for t in tasks
        ]
    return _contract(MatterStatusOut, result)


def _summary_payload(summary: RoundSummary) -> dict:
    return {
        "consensus_points": summary.consensus_points,
        "divergences": summary.divergences,
        "blind_spots": summary.blind_spots,
        "open_questions": summary.open_questions,
        "convergence": summary.convergence,
    }


def _previous_summary_view(session: Session, rnd: Round) -> dict | None:
    """Previous round's ok summary for get_task (PRD 9.2). None for round 1
    or when the previous round has no ok summary; never fabricated."""
    if rnd.round_number <= 1:
        return None
    prev_round = session.scalar(
        select(Round).where(Round.matter_id == rnd.matter_id,
                            Round.round_number == rnd.round_number - 1)
    )
    if prev_round is None:
        return None
    prev = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == prev_round.id,
                                   RoundSummary.generation_status == "ok")
    )
    if prev is None:
        return None
    return {"round_number": prev_round.round_number, **_summary_payload(prev)}


def _resolution_view(session: Session, matter_id: str) -> dict | None:
    """Resolution stage and version for get_matter_status (PRD 9.2). Same
    view for initiator and participants; draft/final bodies stay on the web
    decision page. None when no resolution exists."""
    res = session.scalar(
        select(Resolution)
        .where(Resolution.matter_id == matter_id)
        .order_by(Resolution.version.desc())
        .limit(1)
    )
    if res is None:
        return None
    return {
        "resolution_id": res.id,
        "status": res.status,
        "version": res.version,
        "cited_rounds": res.cited_rounds,
        "created_at": iso_z(res.created_at),
        "decided_at": iso_z(res.decided_at) if res.decided_at else None,
    }


def _round_summaries_view(session: Session, round_id: str) -> list[dict]:
    """Real ok summaries for a round (at most one row by unique constraint);
    visible to initiator AND participants (FR-07). Empty list when absent."""
    ok = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == round_id,
                                   RoundSummary.generation_status == "ok")
    )
    if ok is None:
        return []
    return [{
        "summary_id": ok.id,
        **_summary_payload(ok),
        "created_at": iso_z(ok.created_at),
    }]


# --------------------------------------------------------------------------
# r5Am9i · 立场层 MCP 工具（6/7 已落地；ask_participant 待口径）
# --------------------------------------------------------------------------

# 「未终态」= 非 completed / cancelled（与 set_agent_authority 的终态判定同口径）。
_TERMINAL_MATTER_STATUSES = ("completed", "cancelled")


def _item_brief(matter: Matter) -> dict:
    return {
        "matter_id": matter.id,
        "title": matter.title,
        "status": matter.status,
        "item_version": matter.item_version or 1,
        "irreversible": bool(matter.irreversible),
        "overall_deadline": (iso_z(matter.overall_deadline)
                             if matter.overall_deadline else None),
    }


def mcp_list_items(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    participant: str | None = None,
    state: str | None = None,
) -> dict:
    """list_items：列出本人可见的事项（rpQt6D：`GET /items`）。

    筛选口径（rpQt6D 原文只给了 `?participant=me&state=open` 两个取值，
    其余一律 422，不开自由文本口）：
    - ``participant="me"``：只列**本人是参与人**的事项（仅发起的不算）；
      缺省 = 本人可见的全部（发起 ∪ 参与），即 ``list_matters_for_user``。
    - ``state="open"``：排除终态（completed / cancelled）；缺省 = 不限状态。

    可见性**只由本人身份派生**，不接受调用方传入他人 user_id——没有越权面。
    """
    if participant is not None and participant != "me":
        raise ApiError(422, "VALIDATION_FAILED", "participant 只接受 'me'")
    if state is not None and state != "open":
        raise ApiError(422, "VALIDATION_FAILED", "state 只接受 'open'")

    user = _user(session, user_id)
    matters = matters_svc.list_matters_for_user(session, user=user)
    if participant == "me":
        joined = set(session.scalars(
            select(MatterParticipant.matter_id)
            .where(MatterParticipant.user_id == user.id)
        ).all())
        matters = [m for m in matters if m.id in joined]
    if state == "open":
        matters = [m for m in matters
                   if m.status not in _TERMINAL_MATTER_STATUSES]
    return _contract(
        ItemListOut,
        {"items": [_item_brief(m) for m in matters]},
        mode="json",
    )


def _directed_questions_view(session: Session, *, matter_id: str,
                             target_user_id: int) -> list[dict]:
    """别人问「我」的问题（当前最新轮），挂进任务上下文。空则返回 []。"""
    round_number = session.scalar(
        select(Round.round_number)
        .where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc())
        .limit(1)
    )
    if round_number is None:
        return []
    rows = session.scalars(
        select(ParticipantQuestion)
        .where(ParticipantQuestion.matter_id == matter_id,
               ParticipantQuestion.round_number == round_number,
               ParticipantQuestion.target_user_id == target_user_id)
        .order_by(ParticipantQuestion.created_at)
    ).all()
    return [
        {
            "question_id": q.id,
            "round_number": q.round_number,
            "question": q.question,
            "asked_by_user_id": q.asked_by_user_id,
        }
        for q in rows
    ]


def mcp_list_stances(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
) -> list[dict]:
    """list_stances：本事项下当前用户可见的立场列表（私有字段已裁剪，B27）。

    与 REST 端点 ``GET /api/items/{id}/stances`` 调用**同一个**本函数 ——
    D3 单一事实源。此前该路由直接调 ``stance_svc``，是既存漂移，2026-09-17
    按最新约定回填。

    **不注册为 MCP 工具**：`r5Am9i` 的工具清单是 7 个，没有它。methods 层是
    「唯一实现层」，工具壳是另一层，两者不必一一对应（`get_digest` 也曾长期
    只有 methods 层、没有工具壳）。
    """
    stances = stance_svc.list_stances(session, matter_id=matter_id,
                                      user=_user(session, user_id))
    return [StanceListItem.model_validate(s).model_dump(mode="json")
            for s in stances]


def mcp_read_stance_analysis(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    round_number: int,
):
    """read_stance_analysis：本轮立场分析（只读聚合）。

    与 REST 端点 ``GET /api/items/{id}/stances/analysis`` 同一个本函数
    （D3 单一事实源）。返回 ``RoundStanceAnalysis`` 本体，由路由的
    `response_model` / `_contract` 决定序列化 —— 这里不再套一层形状，
    避免与既有出参漂移。**不注册为 MCP 工具**（理由见上）。
    """
    return stance_svc.analyze_round(
        session, matter_id=matter_id, round_number=round_number,
        user=_user(session, user_id),
    )


def mcp_ask_participant(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    payload: dict,
) -> dict:
    """ask_participant：就某事项向某位参与人发起定向追问（r5Am9i 第 5 个工具）。

    **无配额**（owner 2026-09-14 裁决「先不限制」）—— 本函数不含任何计数 /
    限流 / 429 逻辑，任何为此加计数闸门的改动都违背该裁决。

    守卫全部复用既有件，不重写：
    - 调用方非事项成员 → 404「事项不存在」（``_require_matter``，不泄露存在性）
    - 目标不是该事项参与人 → 422 VALIDATION_FAILED
    - 未给 round_number → 取该事项当前最大轮次；给了但该轮不存在 → 422

    幂等：同一 (事项, 轮次, 被问人, 提问人, 问题正文) 只落一行，重发返回
    首次那一行（``question_hash`` + 唯一约束兜底，不引入外部幂等键）。

    落点说明：问题存 ``participant_questions``，由 ``mcp_get_task`` 在**被问人
    自己的任务上下文**里回传 —— 这才是「待其提交立场时需回答」的投递口。
    没有写进 ``stances.questions_for``（那是「本人向他人提问」，且目标未提交
    立场时该行不存在）。见 ``outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md``。
    """
    try:
        data = AskParticipantIn.model_validate(payload)
    except ValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e

    asker = _user(session, user_id)
    _require_matter(session, matter_id=matter_id, user_id=user_id)

    if not is_participant(session, matter_id=matter_id,
                          user_id=data.target_user_id):
        raise ApiError(422, "VALIDATION_FAILED",
                       "target_user_id 不是该事项的参与人")

    if data.round_number is None:
        round_number = session.scalar(
            select(Round.round_number)
            .where(Round.matter_id == matter_id)
            .order_by(Round.round_number.desc())
            .limit(1)
        )
        if round_number is None:
            raise ApiError(409, "INVALID_STATE_TRANSITION", "该事项尚无轮次")
    else:
        round_number = data.round_number
        exists = session.scalar(
            select(Round.id).where(Round.matter_id == matter_id,
                                   Round.round_number == round_number)
        )
        if exists is None:
            raise ApiError(422, "VALIDATION_FAILED",
                           "round_number 不在该事项的轮次里")

    question_hash = sha256_hex(data.question)
    row = session.scalar(
        select(ParticipantQuestion).where(
            ParticipantQuestion.matter_id == matter_id,
            ParticipantQuestion.round_number == round_number,
            ParticipantQuestion.target_user_id == data.target_user_id,
            ParticipantQuestion.asked_by_user_id == asker.id,
            ParticipantQuestion.question_hash == question_hash,
        )
    )
    if row is None:
        row = ParticipantQuestion(
            matter_id=matter_id,
            round_number=round_number,
            target_user_id=data.target_user_id,
            asked_by_user_id=asker.id,
            question=data.question,
            question_hash=question_hash,
        )
        session.add(row)
        session.flush()

    return _contract(AskParticipantOut, {
        "question_id": row.id,
        "matter_id": row.matter_id,
        "round_number": row.round_number,
        "target_user_id": row.target_user_id,
        "asked_by_user_id": row.asked_by_user_id,
        "question": row.question,
        "created_at": iso_z(row.created_at),
    }, mode="json")


def mcp_declare_item(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    payload: dict,
) -> dict:
    """declare_item：调用方（令牌用户）作为发起人创建事项（items 载体）。"""
    try:
        data = DeclareItemIn.model_validate(payload)
    except ValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e
    initiator = session.get(User, user_id)
    if initiator is None:
        raise ApiError(401, "AUTH_INVALID_TOKEN", "令牌对应用户不存在")
    deadline = None
    if data.overall_deadline is not None:
        try:
            deadline = parse_iso_z(data.overall_deadline)
        except ValueError as e:
            raise ApiError(422, "VALIDATION_FAILED",
                           "overall_deadline 不是合法 ISO 8601 时间") from e
    try:
        matter = matters_svc.create_matter(
            session, initiator=initiator, title=data.title, goal=data.question,
            background=data.background, participant_ids=data.participant_ids,
            initiator_participates=False, timeout_seconds=72 * 3600,
            max_rounds=10, draft_questions=[],
        )
    except ParticipantValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e
    matter.irreversible = data.irreversible
    matter.options = data.options
    matter.overall_deadline = deadline
    matter.item_version = 1
    session.flush()
    return _contract(DeclareItemOut, {
        "matter_id": matter.id,
        "status": matter.status,
        "item_version": matter.item_version,
        "irreversible": matter.irreversible,
    })


def mcp_submit_stance(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    payload: dict,
) -> dict:
    """submit_stance：提交一条立场，入参/出参共用 JSON API 的同源模型。"""
    try:
        data = StanceCreate.model_validate(payload)
    except ValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e
    stance = stance_svc.create_stance(session, matter_id=matter_id,
                                      user=_user(session, user_id),
                                      payload=data)
    return _contract(StanceRead, stance, mode="json")


def mcp_read_stance(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    target_user_id: int,
) -> dict:
    stance = stance_svc.get_stance(session, matter_id=matter_id,
                                   user=_user(session, user_id),
                                   target_user_id=target_user_id)
    return _contract(StanceRead, stance, mode="json")


def mcp_get_summary(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
) -> dict:
    """get_summary：该事项最新一轮 ok 摘要（五字段无身份）。

    读侧闸门：事项成员（发起人 ∪ 参与人）可见；**非成员一律 404**（不用
    403，避免泄露「该事项存在」），与 ``hub/api/stances.py`` 读侧同一口径。
    """
    _require_matter(session, matter_id=matter_id, user_id=user_id)
    row = session.execute(
        select(RoundSummary, Round.round_number)
        .join(Round, RoundSummary.round_id == Round.id)
        .where(RoundSummary.matter_id == matter_id,
               RoundSummary.generation_status == "ok")
        .order_by(Round.round_number.desc())
        .limit(1)
    ).first()
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "暂无已生成的 ok 摘要")
    summary, round_number = row
    return _contract(RoundSummaryOut, {
        "round_id": summary.round_id,
        "round_number": round_number,
        **_summary_payload(summary),
    }, mode="json")


def _digest_decision_view(session: Session, matter_id: str) -> dict | None:
    """最新版本决议的摘要视图；无决议返回 None（键仍在、值 None）。

    `final` 由状态派生（approved / modified / rejected 为已定稿），
    未定稿时 `text` 给 `recommendation`（草案摘要）——PRD-03「pending_review
    时给草案，标注状态」。**不含** user_id / confidence。
    """
    res = session.scalar(
        select(Resolution)
        .where(Resolution.matter_id == matter_id)
        .order_by(Resolution.version.desc())
        .limit(1)
    )
    if res is None:
        return None
    final = res.status in ("approved", "modified", "rejected")
    return {
        "decision_id": res.id,
        "status": res.status,
        "final": final,
        "version": res.version,
        "cited_rounds": res.cited_rounds,
        "text": (res.final_text or res.recommendation) if final
        else res.recommendation,
    }


def _digest_open_items(matter: Matter, summary: RoundSummary) -> list[str]:
    """未决开口：摘要已聚合的 open_questions（去重保序）+ 事项阻塞原因。

    不直接读各参与人立场 —— 见 `mcp_get_digest` docstring 里的口径说明。
    """
    items: list[str] = []
    seen: set[str] = set()
    for q in list(summary.open_questions or []) + (
            [matter.blocked_reason] if matter.blocked_reason else []):
        if isinstance(q, str) and q and q not in seen:
            seen.add(q)
            items.append(q)
    return items


def mcp_get_digest(
    session: Session,
    settings,
    *,
    user_id: int,
    matter_id: str,
) -> dict:
    """get_digest：一页纸现状（裁决 3，owner 2026-09-14 定，09-16 复核「不变」）。

    形状 = 开工包 PRD-03 的三个节：`current_round` / `decision` / `open_items`，
    另保留既有摘要五字段。出参过 `DigestOut` 契约（PRD-03 验收第 2 条：
    手工塞一个 `user_id` 进去必须炸）。

    **历史偏差更正**：`873ff8d` 曾把它落成「最新 ok 摘要 + 状态 + 收敛」的
    「简单形态」，`decision`（最新决议/草案）整节缺失 —— 而那正是「这个事项
    进行到哪了」最该回答的一节。此处补齐。

    `open_items` 的来源说明：PRD-03 原文写「本轮立场的 `open_questions` 去重
    聚合」，但 2026-09-16 的延后公开口径规定「进行中只见本人立场」，digest 直接
    聚合他人立场会绕过那条口径。故取**摘要里已聚合的 `open_questions`**
    （摘要本身即本轮立场的汇总，且五字段无身份），另并入事项的阻塞原因 ——
    口径变更晚于 PRD-03，按最新的来。

    闸门必须在取摘要**之前**：非成员拿到的应是「事项不存在」的 404，而不是
    「暂无摘要」——后者会泄露「该事项存在、只是还没出摘要」。
    """
    matter = _require_matter(session, matter_id=matter_id, user_id=user_id)
    row = session.execute(
        select(RoundSummary, Round.round_number)
        .join(Round, RoundSummary.round_id == Round.id)
        .where(RoundSummary.matter_id == matter_id,
               RoundSummary.generation_status == "ok")
        .order_by(Round.round_number.desc())
        .limit(1)
    ).first()
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "暂无已生成的 ok 摘要")
    summary, round_number = row
    rnd = session.scalar(
        select(Round).where(Round.matter_id == matter_id,
                            Round.round_number == round_number)
    )
    return _contract(DigestOut, {
        "matter_id": matter_id,
        "status": matter.status,
        "current_round": {
            "round_number": round_number,
            "status": rnd.status if rnd is not None else "unknown",
        },
        "decision": _digest_decision_view(session, matter_id),
        "open_items": _digest_open_items(matter, summary),
        **_summary_payload(summary),
    }, mode="json")


def mcp_decide_item(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    matter_id: str,
    payload: dict,
) -> dict:
    """decide_item：发起人对决议草案拍板（FR-21）。

    **本函数是唯一实现。** MCP 工具 ``decide_item`` 与 REST 端点
    ``POST /api/items/{id}/decide`` 都调它 —— test_rest_fallback.py 的 D3
    定死了「两条通道调用同一个 methods 函数、产出经同一组契约」，各写一份
    会让两侧守卫与错误形状各自漂移。

    守卫全部复用 ``resolutions.decide_resolution``，本函数不重复判：
    - 非发起人 → 403 FORBIDDEN_SCOPE
    - irreversible 事项 → 403 FORBIDDEN_SCOPE（不可逆不让通道终裁）
    - 非 awaiting_decision → 409 INVALID_STATE_TRANSITION
    - expected_version 过期 → 409 RESOLUTION_VERSION_CONFLICT
    本函数只多管一件事：**入参形状与枚举**（非法 decision 值在 DecideItemIn
    就 422，永远到不了服务层）。
    """
    try:
        data = DecideItemIn.model_validate(payload)
    except ValidationError as e:
        raise ApiError(422, "VALIDATION_FAILED", str(e)) from e

    resolutions_svc.decide_resolution(
        session,
        matter_id=matter_id,
        actor=_user(session, user_id),
        decision=data.decision,
        expected_version=data.expected_version,
        final_text=data.final_text,
        rationale=data.rationale,
    )
    # decide_resolution 成功即留下决议；下面的 None 分支不可达，留作契约守卫
    # 而不是新增错误码（hub/api/errors.py 明写「Do NOT add others」）。
    view = _resolution_view(session, matter_id)
    if view is None:
        raise ApiError(409, "INVALID_STATE_TRANSITION", "拍板后未找到决议")
    return _contract(ResolutionView, view, mode="json")
