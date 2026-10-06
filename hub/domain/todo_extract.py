"""从留言总结里抽取待办（v21，2026-10-07）。

和:func:`hub.api.board_summary.refresh_summary` 同一次 LLM 调用里完成 ——
提示词多要求一个 ``todos`` 字段而已，**零额外模型开销**。

两条纪律：

1. **抽取失败绝不影响总结**。模型可能返回奇怪的 ``todos``（字符串而不是对象、
   缺字段、超长）。这里一律**跳过**而不是抛异常：总结是主功能，待办是附加，
   不能因为附加功能把主功能带崩。
2. **抽出来的都``needs_confirm=True``**（owner 2026-10-07 决定）。AI 难免把
   「我觉得可以再想想」当成任务，让人点一下确认才转正式。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.db.models import MatterParticipant, Todo, User
from hub.domain.timeutil import utcnow

# 提示词已要求「最多 5 条」，这里再兜一层：模型不听指令时不能让它写爆表。
MAX_TODOS_PER_ROUND = 8
MAX_TITLE_LEN = 200
MAX_DETAIL_LEN = 1000


def _clean_str(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        # 模型偶尔把 title 写成数字/list，str() 后至少还能用
        value = str(value)
    return value.strip()[:limit]


def _resolve_assignee(session: Session, matter_id: str, name: str) -> int | None:
    """把板上称呼解析成 user id。**认不出来就返回 None，不猜。**

    留言板上显示的名字是参与人自报的 :attr:`MatterParticipant.display_name`
    （不是 ``User.username``）—— 提示词里让模型用「他在板上的称呼」，指的就是它。
    所以先在**本事项参与人**范围内按 display_name 找，再退回 username。
    """
    if not name:
        return None
    rows = session.execute(
        select(MatterParticipant.user_id, User.username,
               MatterParticipant.display_name)
        .join(User, User.id == MatterParticipant.user_id)
        .where(MatterParticipant.matter_id == matter_id)
    ).all()
    if not rows:
        return None
    target = name.strip().lower()
    for uid, username, display_name in rows:
        if display_name and display_name.strip().lower() == target:
            return uid
    for uid, username, display_name in rows:
        if username and username.strip().lower() == target:
            return uid
    return None


def extract_todos(
    session: Session,
    *,
    matter_id: str,
    payload,
    created_by: int | None = None,
) -> list[Todo]:
    """把 LLM 输出里的 ``todos`` 数组落库。

    ``payload`` 是 ``complete_json`` 返回的 dict。本函数**不 commit**，
    交回调用方（与 ``refresh_summary`` 的事务边界一致）。

    返回实际创建的 Todo 列表（可能为空）。任何异常都吞掉并返回空 ——
    见模块 docstring 的纪律 1。
    """
    try:
        return _extract_todos_inner(session, matter_id=matter_id,
                                    payload=payload, created_by=created_by)
    except Exception:
        # 抽取是附加功能，不许把总结主流程带崩
        return []


def _extract_todos_inner(
    session: Session,
    *,
    matter_id: str,
    payload,
    created_by: int | None,
) -> list[Todo]:
    raw = payload.get("todos") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    created: list[Todo] = []
    seen_titles: set[str] = set()
    now = utcnow()

    for item in raw[:MAX_TODOS_PER_ROUND]:
        if not isinstance(item, dict):
            continue  # 写成字符串的，跳过
        title = _clean_str(item.get("title"), MAX_TITLE_LEN)
        if not title:
            continue
        # 同一轮里模型可能重复输出同一条
        fingerprint = title.lower()
        if fingerprint in seen_titles:
            continue
        seen_titles.add(fingerprint)

        detail = _clean_str(item.get("detail"), MAX_DETAIL_LEN)
        assignee_id = _resolve_assignee(
            session, matter_id, _clean_str(item.get("assignee"), 64))
        # due_hint 暂不转成 due_at：模型只给「月底前」这种模糊说法，
        # 自己推算具体日期会错到让人不信任。detail 里原话留着，
        # 由人在界面上补日期（第二期）。

        todo = Todo(
            matter_id=matter_id,
            title=title,
            detail=detail or None,
            status="open",
            assignee_id=assignee_id,
            created_by=created_by,
            source="extracted_from_message",
            needs_confirm=True,   # 等人确认才转正式（owner 2026-10-07）
            created_at=now,
            updated_at=now,
        )
        session.add(todo)
        created.append(todo)

    if created:
        session.flush()
    return created