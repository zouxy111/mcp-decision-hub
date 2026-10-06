"""留言板滚动总结：读得快、更新在后台增量跑、每轮落一份文档。

甲方要求（2026-10-05）：

1. 「云端 AI 会实时总结，每次有新的信息进来，就结合之前的总结信息对当前任务
   有个大概的判断；保证速度，不要占用太多的上下文。」
2. 「每轮大模型总结完云端信息，都写一个总结文档。下一次总结只读最新的留言板
   （除非有人提了需求），否则只改动总结后的内容。」

落实成四条纪律：

* **读侧只查库**（:func:`summary_view`）—— 不调模型、不遍历全板，接口毫秒级返回；
  有新留言还没总结完就标 ``stale``，让调用方先用旧的，不阻塞。
* **写侧增量**（:func:`refresh_summary`）—— 只把「上一次总结」和「还没算进去的
  那几条新留言」喂给模型，每次最多 :data:`MAX_DELTA_MESSAGES` 条；没追上就在
  下一轮继续追（``covered_messages`` 是游标）。**不重发全板**，1000 条的板子
  也不会因为总结而爆上下文。
* **每轮落一份文档**（:class:`hub.db.models.BoardSummaryDocument`）—— 一轮总结
  完成就追加一份不可修改的 md 文档，``version`` 递增；文档里「本轮新增的留言」
  单独一节，前面几节是延续下来的存量结论 —— 也就是「只改动总结后的内容」。
* **有人提需求才重读全板**（:func:`request_reread`）—— 常态只读 delta；有人
  明确要求「现在这个总结不对，重读一遍」时置 ``reread_requested``，下一轮从
  第一条留言重新读、重建总结，理由和提出人写进文档。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hub.db.models import (
    BoardSummary,
    BoardSummaryDocument,
    Matter,
    MatterMessage,
    User,
)
from hub.domain import board as board_svc
from hub.domain.timeutil import utcnow
from hub.llm.client import LLMError
from hub.llm.prompts import build_board_summary_prompt

STATUS_EMPTY = "empty"    # 还没有总结（板子太新 / 还没跑过）
STATUS_OK = "ok"          # 总结是最新的
STATUS_STALE = "stale"    # 有新留言还没总结完，先用这一版
STATUS_FAILED = "failed"  # 上一轮生成失败，可继续用 list_messages

SCHEMA_NAME = "board_summary"
# 一次最多喂多少条新留言（省上下文的关键旋钮）
MAX_DELTA_MESSAGES = 40
# 一轮后台任务最多连追几批（1000 条的板子整板重读需要 25 批，这里留足）
MAX_CATCH_UP_ROUNDS = 40

TRIGGER_AUTO = "auto"
TRIGGER_REQUESTED = "requested"
TRIGGER_IMPORTED = "imported"

# 文档里「本轮新增留言」一节的单条摘录上限
_DOC_MESSAGE_EXCERPT = 200


def _blank_view(matter_id: str, total: int) -> dict:
    return {
        "matter_id": matter_id,
        "summary": "",
        "judgement": "",
        "key_points": [],
        "open_questions": [],
        "message_count": total,
        "covered_messages": 0,
        "document_version": 0,
        "document_count": 0,
        "updated_at": None,
        "status": STATUS_EMPTY,
    }


def document_count(session: Session, *, matter_id: str) -> int:
    return int(session.scalar(
        select(func.count()).select_from(BoardSummaryDocument)
        .where(BoardSummaryDocument.matter_id == matter_id)
    ) or 0)


def summary_view(session: Session, *, matter_id: str) -> dict:
    """读侧：只查库。没有总结时返回空壳 + ``status="empty"``。"""
    total = board_svc.message_count(session, matter_id=matter_id)
    row = session.get(BoardSummary, matter_id)
    if row is None:
        # 一条留言都没有 → empty；有留言但还没总结过 → stale（总结还没跟上）
        view = _blank_view(matter_id, total)
        view["status"] = STATUS_EMPTY if total == 0 else STATUS_STALE
        view["document_count"] = document_count(session, matter_id=matter_id)
        return view

    if row.generation_status == "failed":
        status = STATUS_FAILED
    elif row.covered_messages < total:
        status = STATUS_STALE
    else:
        status = STATUS_OK

    return {
        "matter_id": matter_id,
        "summary": row.summary,
        "judgement": row.judgement,
        "key_points": list(row.key_points or []),
        "open_questions": list(row.open_questions or []),
        "message_count": total,
        "covered_messages": row.covered_messages,
        "document_version": int(row.document_version or 0),
        "document_count": document_count(session, matter_id=matter_id),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "status": status,
    }


def is_stale(session: Session, *, matter_id: str) -> bool:
    """有没有「还没算进总结」的新留言（worker 用它决定要不要跑）。"""
    total = board_svc.message_count(session, matter_id=matter_id)
    row = session.get(BoardSummary, matter_id)
    covered = row.covered_messages if row is not None else 0
    if row is not None and row.reread_requested and total > 0:
        return True
    return total > covered


def request_reread(session: Session, *, matter_id: str, user_id: int,
                   reason: str | None = None) -> dict:
    """有人提了需求：下一轮总结**重读全板**（不是增量）。

    只有这块板的成员能提（非成员抛 :class:`BoardError`）。提完之后仍然由后台
    worker 跑：一次最多 40 条地整板重建，每一批都落一份文档，读侧全程可用。
    """
    board_svc.require_member(session, matter_id=matter_id, user_id=user_id)
    total = board_svc.message_count(session, matter_id=matter_id)
    row = session.get(BoardSummary, matter_id)
    if row is None:
        row = BoardSummary(matter_id=matter_id, updated_at=utcnow())
        session.add(row)
    row.reread_requested = True
    row.reread_requested_at = utcnow()
    row.reread_requested_by = user_id
    row.reread_reason = (reason or "").strip()[:500] or None
    # 游标归零 + 状态置 idle：这就是「下一轮重读全板」的开关本身
    # （正文先留着，读侧 status=stale，重建完再被新内容覆盖）。
    row.covered_messages = 0
    row.generation_status = "idle"
    row.error = None
    row.updated_at = utcnow()
    session.flush()
    return {
        "matter_id": matter_id,
        "requested": True,
        "message_count": total,
        "requested_by_user_id": user_id,
        "reason": row.reread_reason,
        "requested_at": row.reread_requested_at.isoformat(),
    }


def _requester_name(session: Session, user_id: int | None) -> str | None:
    if not user_id:
        return None
    user = session.get(User, user_id)
    if user is None:
        return None
    return user.username


def _render_document(*, matter: Matter, version: int, covered_from: int,
                     covered_to: int, delta_messages: list[dict],
                     summary: str, judgement: str, key_points: list[str],
                     open_questions: list[str], trigger: str,
                     requester: str | None, reason: str | None) -> str:
    """把这一轮总结渲染成一份可下载的 markdown 文档。

    「本轮新增」这一节是本轮真正变化的部分（只改动总结后的内容）；上面几节
    是从上一版延续下来的存量结论。
    """
    lines: list[str] = []
    lines.append(f"# {matter.title} · 云端总结 v{version}")
    lines.append("")
    lines.append(
        f"> 生成时间：{utcnow().isoformat()}Z ｜ 覆盖留言：第 {covered_from}–"
        f"{covered_to} 条（本轮新增 {len(delta_messages)} 条）"
    )
    if trigger == TRIGGER_REQUESTED:
        who = f"（由 {requester} 提出）" if requester else ""
        lines.append(f"> 触发方式：**有人提了需求，重读全板后重建**{who}")
        if reason:
            lines.append(f"> 提出时的理由：{reason}")
    else:
        lines.append("> 触发方式：有新留言，增量更新（只读了上次总结 + 新增留言）")
    lines.append("> 这份文档由云端 AI 自动生成，不是板上任何人的结论；"
                 "要引用谁的观点请回原留言核对。")
    lines.append("")
    lines.append("## 当前进展")
    lines.append("")
    lines.append(summary or "（还没写）")
    lines.append("")
    lines.append("## 对当前任务的判断")
    lines.append("")
    lines.append(judgement or "（还没写）")
    lines.append("")
    lines.append("## 已经明确的事实 / 结论")
    lines.append("")
    if key_points:
        lines.extend(f"- {item}" for item in key_points)
    else:
        lines.append("（暂无）")
    lines.append("")
    lines.append("## 还没解决的问题")
    lines.append("")
    if open_questions:
        lines.extend(f"- {item}" for item in open_questions)
    else:
        lines.append("（暂无）")
    lines.append("")
    lines.append(f"## 本轮新增的留言（第 {covered_from}–{covered_to} 条）")
    lines.append("")
    if not delta_messages:
        lines.append("（本轮没有新增留言）")
    for m in delta_messages:
        who = m.get("display_name") or m.get("username") or "某人"
        role = m.get("responsibility")
        head = f"- **{who}**"
        if role:
            head += f"（{role}）"
        kind = m.get("kind")
        if kind and kind != "message":
            head += f" · {kind}"
        body = (m.get("content") or "").strip().replace("\n", " ")
        if len(body) > _DOC_MESSAGE_EXCERPT:
            body = body[:_DOC_MESSAGE_EXCERPT] + "…"
        attachment = m.get("attachment_name")
        if attachment:
            body = (body + f"（附件：{attachment}）").strip()
        lines.append(f"{head}：{body or '（无正文）'}")
    lines.append("")
    return "\n".join(lines)


def list_documents(session: Session, *, matter_id: str,
                   limit: int | None = None) -> list[dict]:
    """这块板的总结文档清单（不含正文，省上下文），最新在前。"""
    stmt = (
        select(BoardSummaryDocument)
        .where(BoardSummaryDocument.matter_id == matter_id)
        .order_by(BoardSummaryDocument.version.desc())
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    rows = list(session.scalars(stmt).all())
    return [{
        "version": row.version,
        "created_at": row.created_at.isoformat(),
        "covered_from": row.covered_from,
        "covered_to": row.covered_to,
        "delta_messages": row.delta_messages,
        "trigger": row.trigger,
        "summary": row.summary,
        "document_chars": len(row.content_md or ""),
    } for row in rows]


def get_document(session: Session, *, matter_id: str,
                 version: int | None = None) -> dict | None:
    """取一份总结文档（``version=None`` = 最新一份）；没有就返回 None。"""
    stmt = select(BoardSummaryDocument).where(
        BoardSummaryDocument.matter_id == matter_id)
    if version is None:
        stmt = stmt.order_by(BoardSummaryDocument.version.desc()).limit(1)
    else:
        stmt = stmt.where(BoardSummaryDocument.version == version)
    row = session.scalar(stmt)
    if row is None:
        return None
    return {
        "matter_id": matter_id,
        "version": row.version,
        "created_at": row.created_at.isoformat(),
        "covered_from": row.covered_from,
        "covered_to": row.covered_to,
        "delta_messages": row.delta_messages,
        "trigger": row.trigger,
        "summary": row.summary,
        "judgement": row.judgement,
        "key_points": list(row.key_points or []),
        "open_questions": list(row.open_questions or []),
        "content_md": row.content_md,
    }


def refresh_summary(session: Session, *, llm, matter_id: str) -> str:
    """增量刷新一块板的总结，并把这一轮落成一份文档。

    返回 ``ok`` / ``up_to_date`` / ``no_matter`` / ``failed`` / ``empty_board``。
    调用方负责 commit。LLM 调用是阻塞的 —— 线上由后台 worker 线程调用。
    """
    matter = session.get(Matter, matter_id)
    if matter is None:
        return "no_matter"

    total = board_svc.message_count(session, matter_id=matter_id)
    row = session.get(BoardSummary, matter_id)

    # 「有人提了需求」→ 这一轮从第一条留言重新读，不是增量。
    # 整板重读要分好几批（每批 MAX_DELTA_MESSAGES 条），所以只有**第一批**
    # 从头开始；后面的批次接着重建，并继续标成 requested（下面的 still_rebuilding）。
    trigger = TRIGGER_AUTO
    requester = None
    reason = None
    first_rebuild_batch = False
    if row is not None and row.reread_requested:
        trigger = TRIGGER_REQUESTED
        requester = _requester_name(session, row.reread_requested_by)
        reason = row.reread_reason
        row.reread_requested = False
        if row.covered_messages == 0:
            # 第一批：不吃旧总结，从零重建
            first_rebuild_batch = True
            row.summary = ""
            row.judgement = ""
            row.key_points = []
            row.open_questions = []
            row.generation_status = "idle"
        row.error = None
        session.flush()

    covered = row.covered_messages if row is not None else 0
    if row is not None and row.generation_status == "ok" and covered >= total:
        return "up_to_date"
    if total == 0:
        return "empty_board"

    # 还没算进总结的留言，按时间正序取前 MAX_DELTA_MESSAGES 条
    rows = list(session.scalars(
        select(MatterMessage)
        .where(MatterMessage.matter_id == matter_id)
        .order_by(MatterMessage.created_at, MatterMessage.id)
        .offset(covered)
        .limit(MAX_DELTA_MESSAGES)
    ).all())
    if not rows:
        return "up_to_date"

    # 增量输入：上一版总结（很小）+ 这一批新留言。
    # 整板重读的第一批不带旧总结（那正是「重读」的意思）；后续批次带上
    # 已经重建出来的部分，接着往下写。
    previous = None
    if not first_rebuild_batch and row is not None and (row.summary or row.judgement):
        previous = {
            "summary": row.summary,
            "judgement": row.judgement,
            "key_points": list(row.key_points or []),
            "open_questions": list(row.open_questions or []),
        }
    new_messages = [board_svc.message_view(session, m) for m in rows]

    system_prompt, user_prompt = build_board_summary_prompt(
        title=matter.title, goal=matter.goal, background=matter.background,
        previous=previous, new_messages=new_messages,
    )
    try:
        data = llm.complete_json(system_prompt, user_prompt,
                                 schema_name=SCHEMA_NAME)
    except LLMError as e:
        # 失败不动旧总结：读侧继续用上一版，只是状态标 failed。
        # （整板重读失败时 live 总结是空的，但历史文档还在
        #   board_summary_documents 里，一份都不会丢。）
        if row is None:
            row = BoardSummary(matter_id=matter_id, updated_at=utcnow())
            session.add(row)
        row.generation_status = "failed"
        row.error = getattr(e, "error_code", None) or str(e)
        if trigger == TRIGGER_REQUESTED:
            # 有人提的重读需求不能被一次失败吞掉：挂回去，下一轮再试
            row.reread_requested = True
        row.updated_at = utcnow()
        session.flush()
        return "failed"

    if row is None:
        row = BoardSummary(matter_id=matter_id, updated_at=utcnow())
        session.add(row)

    def _text(key: str, limit: int) -> str:
        value = data.get(key)
        return str(value).strip()[:limit] if value else ""

    def _list(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:300] for item in value if str(item).strip()][:8]

    summary = _text("summary", 4000)
    judgement = _text("judgement", 2000)
    key_points = _list("key_points")
    open_questions = _list("open_questions")
    covered_to = covered + len(rows)
    # 版本号取「已有文档里的最大值 + 1」：比只信 row.document_version 稳，
    # 万一有人手工补过历史文档也不会撞 (matter_id, version) 唯一约束。
    version = int(session.scalar(
        select(func.max(BoardSummaryDocument.version))
        .where(BoardSummaryDocument.matter_id == matter_id)
    ) or int(row.document_version or 0)) + 1

    # 「每轮总结完都写一个总结文档」：只追加、不覆盖历史版本
    document = BoardSummaryDocument(
        matter_id=matter_id,
        version=version,
        covered_from=covered + 1,
        covered_to=covered_to,
        delta_messages=len(rows),
        summary=summary,
        judgement=judgement,
        key_points=key_points,
        open_questions=open_questions,
        content_md=_render_document(
            matter=matter, version=version, covered_from=covered + 1,
            covered_to=covered_to, delta_messages=new_messages,
            summary=summary, judgement=judgement, key_points=key_points,
            open_questions=open_questions, trigger=trigger,
            requester=requester, reason=reason,
        ),
        trigger=trigger,
        created_at=utcnow(),
    )
    session.add(document)

    row.summary = summary
    row.judgement = judgement
    row.key_points = key_points
    row.open_questions = open_questions
    row.covered_messages = covered_to
    row.document_version = version
    row.generation_status = "ok"
    row.error = None
    # 整板重读还没读完（比如 1000 条要 25 批）：把开关挂回去，
    # 让剩下的批次也标成 requested，下一轮接着重建到底。
    if trigger == TRIGGER_REQUESTED and covered_to < total:
        row.reread_requested = True
    row.updated_at = utcnow()
    session.flush()
    return "ok"
