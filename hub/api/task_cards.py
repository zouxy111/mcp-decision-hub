"""任务卡服务（v22，2026-10-10）：讨论验收标准 → 发布 → 交付（AI 软审查）→ 验收。

背景：owner 2026-10-10 裁定补「任务下发→开工→交付验收」协同质量链路
（测试反馈：杨琦 2026-10-08~09）。设计要点：

* **验收标准先讨论后发布**：卡以 draft 落地，发布人可反复改；确认发布后
  锁定（改标准 = 关旧卡开新卡），保证「当前有效口径」可考（反馈第 6 条）。
* **软门禁**：交付提交时 AI 对照验收标准逐项审查并指出缺陷，**不阻断**；
  最终 accept / reject 由发布人拍（owner：不强制，但要告诉哪里有缺陷）。
* 纯函数与状态机在 :mod:`hub.domain.task_cards`；本模块只管 CRUD、
  成员权限与 LLM 审查编排。
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api.errors import ApiError
from hub.api.matters import is_participant
from hub.db.models import Matter, TaskCard, TaskDelivery, User
from hub.domain import task_cards as cards
from hub.domain.timeutil import utcnow
from hub.llm.runtime import RuntimeLlm


def _require_member(session: Session, *, matter_id: str, user_id: int) -> Matter:
    """成员校验（发起人或参与人）；非成员一律 404 不泄露存在性。"""
    matter = session.get(Matter, matter_id)
    if matter is None or matter.initiator_id != user_id and not is_participant(
            session, matter_id=matter_id, user_id=user_id):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "事项不存在或不可见")
    return matter


def _get_card(session: Session, *, card_id: str) -> TaskCard:
    card = session.get(TaskCard, card_id)
    if card is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "任务卡不存在")
    return card


def list_cards(session: Session, *, matter_id: str, user_id: int) -> list[TaskCard]:
    _require_member(session, matter_id=matter_id, user_id=user_id)
    return list(session.scalars(
        select(TaskCard).where(TaskCard.matter_id == matter_id)
        .order_by(TaskCard.created_at)
    ).all())


def get_card(session: Session, *, card_id: str, user_id: int) -> TaskCard:
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=user_id)
    return card


def create_card(
    session: Session,
    *,
    matter_id: str,
    publisher: User,
    title: str,
    description: str = "",
    acceptance_criteria: list[str] | None = None,
) -> TaskCard:
    _require_member(session, matter_id=matter_id, user_id=publisher.id)
    title = (title or "").strip()
    if not title:
        raise ApiError(422, "VALIDATION_FAILED", "任务标题为必填项")
    card = TaskCard(
        matter_id=matter_id,
        title=title[:cards.MAX_TITLE_LEN],
        description=(description or "").strip()[:cards.MAX_DESCRIPTION_LEN],
        acceptance_criteria=cards.dump_criteria(
            cards.validate_criteria(acceptance_criteria or [])),
        status="draft",
        publisher_id=publisher.id,
    )
    session.add(card)
    session.flush()
    audit.record_audit(session, audit.TASK_CARD_CREATED,
                       actor_user_id=publisher.id, matter_id=matter_id,
                       detail={"card_id": card.id, "title": card.title})
    session.flush()
    return card


def update_card(
    session: Session,
    *,
    card_id: str,
    actor: User,
    title: str | None = None,
    description: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> TaskCard:
    """改卡。**仅 draft 可改**（owner 2026-10-10：发布即锁定口径）。"""
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=actor.id)
    if card.publisher_id != actor.id:
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发布人可修改任务卡")
    if card.status != "draft":
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "任务卡已发布，验收标准锁定；如需变更请关闭后新建")
    changed: dict = {}
    if title is not None:
        card.title = (title or "").strip()[:cards.MAX_TITLE_LEN] or card.title
        changed["title"] = True
    if description is not None:
        card.description = (description or "").strip()[:cards.MAX_DESCRIPTION_LEN]
        changed["description"] = True
    if acceptance_criteria is not None:
        card.acceptance_criteria = cards.dump_criteria(
            cards.validate_criteria(acceptance_criteria))
        changed["criteria"] = True
    audit.record_audit(session, audit.TASK_CARD_UPDATED,
                       actor_user_id=actor.id, matter_id=card.matter_id,
                       detail={"card_id": card.id, **changed})
    session.flush()
    return card


def publish_card(session: Session, *, card_id: str, actor: User) -> TaskCard:
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=actor.id)
    if card.publisher_id != actor.id:
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发布人可确认发布")
    cards.assert_card_transition(card.status, "published")
    criteria = cards.parse_criteria(card.acceptance_criteria)
    if not criteria:
        raise ApiError(422, "VALIDATION_FAILED",
                       "验收标准为空：先跟发布人把「做到什么程度算过」逐条写清楚再发布")
    card.status = "published"
    card.published_at = utcnow()
    audit.record_audit(session, audit.TASK_CARD_PUBLISHED,
                       actor_user_id=actor.id, matter_id=card.matter_id,
                       detail={"card_id": card.id,
                               "criteria_count": len(criteria)})
    session.flush()
    return card


def close_card(session: Session, *, card_id: str, actor: User) -> TaskCard:
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=actor.id)
    if card.publisher_id != actor.id:
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发布人可关闭任务卡")
    cards.assert_card_transition(card.status, "closed")
    card.status = "closed"
    audit.record_audit(session, audit.TASK_CARD_CLOSED,
                       actor_user_id=actor.id, matter_id=card.matter_id,
                       detail={"card_id": card.id})
    session.flush()
    return card


def list_deliveries(session: Session, *, card_id: str,
                    user_id: int) -> list[TaskDelivery]:
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=user_id)
    return list(session.scalars(
        select(TaskDelivery).where(TaskDelivery.card_id == card_id)
        .order_by(TaskDelivery.created_at.desc())
    ).all())


def submit_delivery(
    session: Session,
    *,
    card_id: str,
    user: User,
    summary: str,
    self_check: list[dict] | None = None,
    llm: RuntimeLlm | None = None,
) -> TaskDelivery:
    """交付一件活。落库 + AI 软审查（缺陷只提醒，不阻断）。

    LLM 审查失败不影响交付落库（review_status="failed"）——软门禁的底线是
    交付不能卡在 AI 手上。
    """
    card = _get_card(session, card_id=card_id)
    _require_member(session, matter_id=card.matter_id, user_id=user.id)
    if card.status != "published":
        raise ApiError(409, "INVALID_STATE_TRANSITION",
                       "任务卡未发布（或已关闭），不能提交交付")
    summary = (summary or "").strip()
    if not summary:
        raise ApiError(422, "VALIDATION_FAILED", "交付说明为必填项")
    criteria = cards.parse_criteria(card.acceptance_criteria)
    delivery = TaskDelivery(
        card_id=card.id,
        matter_id=card.matter_id,
        submitter_id=user.id,
        summary=summary[:cards.MAX_SUMMARY_LEN],
        self_check=json.dumps(
            cards.normalize_self_check(self_check, criteria), ensure_ascii=False),
        status="submitted",
    )
    session.add(delivery)
    session.flush()
    delivery.ai_review, delivery.review_status = review_delivery(card, delivery,
                                                                 llm)
    audit.record_audit(session, audit.TASK_DELIVERY_SUBMITTED,
                       actor_user_id=user.id, matter_id=card.matter_id,
                       detail={"card_id": card.id, "delivery_id": delivery.id,
                               "review_status": delivery.review_status})
    session.flush()
    return delivery


def review_delivery(card: TaskCard, delivery: TaskDelivery,
                    llm: RuntimeLlm | None = None) -> tuple[str | None, str]:
    """AI 对照验收标准审查交付说明 → (ai_review JSON, review_status)。

    独立于 submit 以便测试桩直接调用。LLM 不可用时返回 (None, "failed")。
    """
    if llm is None:
        return None, "failed"
    criteria = cards.parse_criteria(card.acceptance_criteria)
    checks = cards.parse_self_check(delivery.self_check)
    check_lines = "\n".join(
        f"- {c['criterion']}：{'符合' if c.get('met') is True else '不符合' if c.get('met') is False else '未自评'}"
        f"{('（' + c.get('note', '') + '）') if c.get('note') else ''}"
        for c in checks)
    user_prompt = f"""任务：{card.title}

任务要求：{card.description or '无'}

验收标准（逐条）：
{chr(10).join(f'{i + 1}. {c}' for i, c in enumerate(criteria))}

交付人说明：{delivery.summary}

交付人自评（逐条对照验收标准）：
{check_lines or '（未提供自评）'}

请逐条对照验收标准审查这份交付说明。verdict 只能是 pass（交付说明已覆盖该标准）/
gap（明显没覆盖或与标准冲突）/ unclear（说明含糊、无法判断）。
按以下 JSON 形状返回（verdicts 与验收标准逐条对应，criterion 填标准原文）：
{{"verdicts": [{{"criterion": "标准原文", "verdict": "pass/gap/unclear",
"gap": "缺口或待补充说明（pass 时填空字符串"}}], "overall": "pass 或 gaps"}}

overall 取整单结论：任一 gap 为 gaps，否则 pass。"""
    try:
        data = llm.complete_json(
            # DeepSeek 要求 prompt 里出现 "json" 字样才允许 json_object 输出
            "你是严格的交付验收审查员。只依据给定材料判断，不臆测。"
            "按 JSON 契约返回逐项结论。",
            user_prompt, schema_name="task_delivery_review")
        verdicts = [v for v in data.get("verdicts", []) if isinstance(v, dict)]
        review = {"verdicts": verdicts, "overall": data.get("overall", "")}
        return json.dumps(review, ensure_ascii=False), review["overall"] or "failed"
    except Exception:
        return None, "failed"


def decide_delivery(
    session: Session,
    *,
    delivery_id: str,
    actor: User,
    accept: bool,
    note: str = "",
) -> TaskDelivery:
    """发布人验收：accept / reject（附理由）。"""
    delivery = session.get(TaskDelivery, delivery_id)
    if delivery is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "交付不存在")
    card = _get_card(session, card_id=delivery.card_id)
    _require_member(session, matter_id=card.matter_id, user_id=actor.id)
    if card.publisher_id != actor.id:
        raise ApiError(403, "FORBIDDEN_SCOPE", "仅发布人可验收交付")
    target = "accepted" if accept else "rejected"
    cards.assert_delivery_decision(delivery.status, target)
    delivery.status = target
    delivery.reviewer_note = (note or "").strip() or None
    delivery.decided_at = utcnow()
    audit.record_audit(session, audit.TASK_DELIVERY_DECIDED,
                       actor_user_id=actor.id, matter_id=card.matter_id,
                       detail={"card_id": card.id, "delivery_id": delivery.id,
                               "decision": target})
    session.flush()
    return delivery
