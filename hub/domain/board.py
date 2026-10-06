"""留言板协作：发言、读回、参与人名片、定向提问。

形态改造（2026-10-04）：协作从「一轮一轮下发任务」改为**留言板**。
2026-10-05 按甲方要求再加三件事：

* **上传前必须本人同意** —— ``acting_as="agent_on_behalf"`` 的发言必须带
  ``human_approved_at``（没带直接拒绝），板上显示「本人已确认」。
* **可以传 md 文件** —— ``attachment_name`` + ``attachment_md`` 全文存库，
  云端直接可读（不落磁盘文件）。
* **云端提问 → 本地处理 → 传回回答** —— ``kind="question"`` 指向某位参与人，
  他的 Agent 用 :func:`list_pending_questions` 拉到本地，问过本人拿到同意后
  用 ``reply_to_message_id`` 把回答传回来，原提问自动置为 answered。

一块板最多 :data:`MAX_MESSAGES_PER_BOARD` 条留言（2026-10-05 起 1000 条，
参与人上限 20 人，见 :mod:`hub.api.matters`）。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hub.db.models import Matter, MatterMessage, MatterParticipant, User
from hub.domain.timeutil import utcnow

# 单条正文上限：留言板不是文档库，长内容应该走 md 附件。
MAX_MESSAGE_CHARS = 8000
# 单个 md 附件上限（字符数，约 200KB 中文）。
MAX_ATTACHMENT_CHARS = 200_000
# 一块留言板的容量上限（2026-10-05 甲方要求：放到 1000 条）。
MAX_MESSAGES_PER_BOARD = 1000

KINDS = frozenset({"message", "decision", "question", "answer"})
ACTING_AS = frozenset({"human", "agent_on_behalf"})
ATTACHMENT_SUFFIXES = (".md", ".markdown", ".txt")


class BoardError(ValueError):
    """留言板操作失败（非成员 / 内容为空 / 超长 / 枚举非法 / 板满 / 未获同意）。"""


def require_member(session: Session, *, matter_id: str, user_id: int) -> Matter:
    """成员闸门：发起人 ∪ 参与人放行，其余抛 :class:`BoardError`。"""
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise BoardError("事项不存在")
    if matter.initiator_id == user_id:
        return matter
    if session.get(MatterParticipant, (matter_id, user_id)) is None:
        raise BoardError("你不是该事项的参与人")
    return matter


def member_ids(session: Session, *, matter_id: str) -> set[int]:
    """板上所有人的 user_id（发起人 ∪ 参与人）。"""
    matter = session.get(Matter, matter_id)
    ids = set(session.scalars(
        select(MatterParticipant.user_id).where(
            MatterParticipant.matter_id == matter_id)
    ).all())
    if matter is not None:
        ids.add(matter.initiator_id)
    return ids


def message_count(session: Session, *, matter_id: str) -> int:
    return int(session.scalar(
        select(func.count()).select_from(MatterMessage)
        .where(MatterMessage.matter_id == matter_id)
    ) or 0)


def post_message(
    session: Session,
    *,
    matter_id: str,
    user_id: int,
    content: str,
    kind: str = "message",
    acting_as: str = "human",
    human_approved_at=None,
    attachment_name: str | None = None,
    attachment_md: str | None = None,
    reply_to_message_id: str | None = None,
    ask_user_id: int | None = None,
) -> MatterMessage:
    """写一条留言（或提问 / 回答）。

    ``kind="decision"`` 仅发起人可用；``kind="question"`` 必须给 ``ask_user_id``；
    带 ``reply_to_message_id`` 时会把对应提问标记为已回答。
    """
    matter = require_member(session, matter_id=matter_id, user_id=user_id)

    text = (content or "").strip()
    attachment_name = (attachment_name or "").strip() or None
    attachment_md = attachment_md if attachment_md and attachment_md.strip() else None

    if not text and attachment_md is None:
        raise BoardError("留言内容不能为空")
    if len(text) > MAX_MESSAGE_CHARS:
        raise BoardError(f"留言过长（上限 {MAX_MESSAGE_CHARS} 字，"
                         "长内容请整理成 md 文件上传）")
    if attachment_md is not None:
        if len(attachment_md) > MAX_ATTACHMENT_CHARS:
            raise BoardError(f"md 附件过大（上限 {MAX_ATTACHMENT_CHARS} 字符）")
        if not attachment_name:
            attachment_name = "附件.md"
        if not attachment_name.lower().endswith(ATTACHMENT_SUFFIXES):
            raise BoardError("只支持上传 .md / .markdown / .txt 文件")
    if kind not in KINDS:
        raise BoardError(f"kind 只接受 {sorted(KINDS)}")
    if acting_as not in ACTING_AS:
        raise BoardError(f"acting_as 只接受 {sorted(ACTING_AS)}")

    # 上传前必须本人同意：Agent 代发不给留痕就拒绝（甲方硬要求）。
    if acting_as == "agent_on_behalf" and human_approved_at is None:
        raise BoardError(
            "Agent 代发必须先获得本人同意：把要上传的原文给本人看过、"
            "拿到明确同意后，再带 human_approved=true 上传"
        )
    if human_approved_at is None:
        human_approved_at = utcnow()

    if message_count(session, matter_id=matter_id) >= MAX_MESSAGES_PER_BOARD:
        raise BoardError(
            f"这块留言板已经满 {MAX_MESSAGES_PER_BOARD} 条了，不能再发"
        )

    if kind == "decision" and matter.initiator_id != user_id:
        raise BoardError("只有发起人可以发布结论")

    if kind == "question":
        if not ask_user_id:
            raise BoardError("提问必须指定问给谁（ask_user_id）")
        if ask_user_id not in member_ids(session, matter_id=matter_id):
            raise BoardError("提问对象不是这块板的参与人")
        question_status = "open"
    else:
        ask_user_id = None
        question_status = None

    if reply_to_message_id:
        target = session.get(MatterMessage, reply_to_message_id)
        if target is None or target.matter_id != matter_id:
            raise BoardError("要回复的留言不存在")
        if target.kind == "question" and target.question_status == "open":
            target.question_status = "answered"
        if kind == "message":
            kind = "answer"

    message = MatterMessage(
        matter_id=matter_id,
        user_id=user_id,
        content=text,
        kind=kind,
        acting_as=acting_as,
        human_approved_at=human_approved_at,
        attachment_name=attachment_name,
        attachment_md=attachment_md,
        asked_to_user_id=ask_user_id,
        reply_to_message_id=reply_to_message_id,
        question_status=question_status,
        created_at=utcnow(),
    )
    session.add(message)
    session.flush()
    return message


def _author_cards(session: Session, *, matter_id: str,
                  user_ids: Iterable[int]) -> dict[int, dict]:
    """一次查出多条留言的作者名片（姓名 / 负责什么）。

    读回一块板原来会按「每条留言 2 条 SQL」（作者 + 参与人名片）发问，1000 条
    的板子就是 2000 条 SQL —— 这是读侧最大的浪费。这里改成一条 JOIN 查全，
    调用方按 user_id 取用。
    """
    unique_ids = {int(uid) for uid in user_ids if uid is not None}
    if not unique_ids:
        return {}
    rows = session.execute(
        select(User, MatterParticipant)
        .outerjoin(
            MatterParticipant,
            (MatterParticipant.user_id == User.id)
            & (MatterParticipant.matter_id == matter_id),
        )
        .where(User.id.in_(unique_ids))
    ).all()
    cards: dict[int, dict] = {}
    for user, participant in rows:
        cards[user.id] = {
            "user_id": user.id,
            "username": user.username,
            "display_name": (participant.display_name if participant else None),
            "responsibility": (participant.responsibility if participant else None),
        }
    # JOIN 没命中的（用户被删等异常数据）也要有形状，不能让调用方 KeyError
    for uid in unique_ids:
        cards.setdefault(uid, {"user_id": uid, "username": None,
                               "display_name": None, "responsibility": None})
    return cards


def _author_card(session: Session, *, matter_id: str, user_id: int) -> dict:
    return _author_cards(session, matter_id=matter_id, user_ids=[user_id])[user_id]


def _message_view_with_card(message: MatterMessage, card: dict) -> dict:
    """用已经查好的作者名片拼一条留言的对外形状（不再查库）。"""
    return {
        "message_id": message.id,
        "matter_id": message.matter_id,
        "kind": message.kind,
        "acting_as": message.acting_as,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "human_approved": message.human_approved_at is not None,
        "human_approved_at": (message.human_approved_at.isoformat()
                              if message.human_approved_at else None),
        "attachment_name": message.attachment_name,
        "attachment_md": message.attachment_md,
        "asked_to_user_id": message.asked_to_user_id,
        "reply_to_message_id": message.reply_to_message_id,
        "question_status": message.question_status,
        **card,
    }


def message_view(session: Session, message: MatterMessage) -> dict:
    """单条留言的对外形状（Web / REST / MCP 共用同一口径）。"""
    card = _author_card(session, matter_id=message.matter_id,
                        user_id=message.user_id)
    return _message_view_with_card(message, card)


def list_messages(
    session: Session,
    *,
    matter_id: str,
    user_id: int,
    limit: int | None = None,
) -> list[dict]:
    """按时间正序读回留言（``limit`` 取**最近** N 条，返回仍是正序）。

    权限与读取同源：非成员直接抛 :class:`BoardError`，不返回空列表
    ——空列表无法区分「没有留言」和「无权查看」。

    查询数固定为 3 条（成员闸门 + 留言 + 作者名片 JOIN），与留言条数无关。
    """
    require_member(session, matter_id=matter_id, user_id=user_id)
    stmt = (
        select(MatterMessage)
        .where(MatterMessage.matter_id == matter_id)
        .order_by(MatterMessage.created_at.desc(), MatterMessage.id.desc())
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    rows = list(session.scalars(stmt).all())
    rows.reverse()
    cards = _author_cards(session, matter_id=matter_id,
                          user_ids=[m.user_id for m in rows])
    return [_message_view_with_card(m, cards[m.user_id]) for m in rows]


def list_pending_questions(
    session: Session,
    *,
    user_id: int,
    matter_id: str | None = None,
) -> list[dict]:
    """云端向「你」提的、还没回答的问题（本地 Agent 的待办入口）。

    只返回 ``asked_to_user_id == user_id`` 且 ``question_status == "open"`` 的提问；
    跨事项时带事项标题，便于本地一次性拉全。
    """
    stmt = (
        select(MatterMessage)
        .where(MatterMessage.asked_to_user_id == user_id,
               MatterMessage.kind == "question",
               MatterMessage.question_status == "open")
        .order_by(MatterMessage.created_at)
    )
    if matter_id:
        stmt = stmt.where(MatterMessage.matter_id == matter_id)
    out = []
    for message in session.scalars(stmt).all():
        matter = session.get(Matter, message.matter_id)
        view = message_view(session, message)
        view["matter_title"] = matter.title if matter else None
        out.append(view)
    return out


def participant_cards(session: Session, *, matter_id: str) -> list[dict]:
    """参与人名片（含还没在本事项发过言的受邀人）。"""
    rows = session.execute(
        select(MatterParticipant, User)
        .join(User, User.id == MatterParticipant.user_id)
        .where(MatterParticipant.matter_id == matter_id)
        .order_by(MatterParticipant.user_id)
    ).all()
    cards = []
    for participant, user in rows:
        cards.append({
            "user_id": user.id,
            "username": user.username,
            "display_name": participant.display_name,
            "responsibility": participant.responsibility,
        })
    return cards
