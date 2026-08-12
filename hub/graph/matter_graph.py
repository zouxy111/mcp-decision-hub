"""LangGraph matter orchestration graph (design §6, M3).

Nodes are THIN WRAPPERS over the pipeline phase functions in
hub.api.pipeline — all business rules stay in domain/pipeline. Each node
opens its own session and commits. thread_id = matter_id; checkpoint tables
share the business SQLite file (PRD 10.3).

Drive model (verified semantics — see the M3 plan's LangGraph section):
- fresh thread            → invoke({"matter_id": ...}, config)
- crash between nodes     → invoke(None, config) continues pending nodes
- paused at an interrupt  → plain invoke would RESTART from START; skip
"""

import logging
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from typing_extensions import TypedDict

from hub.api.pipeline import (
    _branch_phase,
    _draft_resolution_phase,
    _generate_first_round_phase,
    _summarize_phase,
)
from hub.config import Settings
from hub.db.models import Matter, Resolution, Round, RoundSummary
from hub.domain.convergence import (
    CONVERGENCE_CONVERGED,
    CONVERGENCE_PROVISIONALLY_READY,
)
from hub.domain.resolution import (
    RESOLUTION_STATUS_PENDING_REVIEW,
    RESOLUTION_TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)

NODE_GENERATE_ROUND = "generate_round"
NODE_SUMMARIZE = "summarize"
NODE_BRANCH = "branch"
NODE_DRAFT_RESOLUTION = "draft_resolution"


class MatterGraphState(TypedDict, total=False):
    matter_id: str
    branch: str       # set by node_branch: "draft" | "propagate" | "done"
    after_draft: str  # set by node_draft_resolution (task 9 adds gate routes)


def sqlite_path_from_url(database_url: str) -> str:
    """sqlite:///PATH → PATH. Checkpoint tables must live in the business
    database file (PRD 10.3); in-memory databases are not supported."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"不支持的 database_url: {database_url}")
    path = database_url[len(prefix):]
    if path == ":memory:":
        raise ValueError("checkpoint 需要文件型 SQLite，不支持 :memory:")
    return path


def _latest_round(session, matter_id: str) -> Round | None:
    return session.scalar(
        select(Round).where(Round.matter_id == matter_id)
        .order_by(Round.round_number.desc()).limit(1)
    )


def _latest_resolution(session, matter_id: str) -> Resolution | None:
    return session.scalar(
        select(Resolution).where(Resolution.matter_id == matter_id)
        .order_by(Resolution.version.desc()).limit(1)
    )


def _compute_branch_route(session, matter: Matter) -> str:
    """Route after the branch phase. Business tables are the single source of
    truth — the route is recomputed from the DB on every tick."""
    if matter.status not in ("in_progress", "awaiting_decision"):
        return "done"
    rnd = _latest_round(session, matter.id)
    if rnd is None or rnd.status != "closed":
        return "done"
    latest_res = _latest_resolution(session, matter.id)
    if (
        latest_res is not None
        and latest_res.status in RESOLUTION_TERMINAL_STATUSES
        and latest_res.source_round_id == rnd.id
    ):
        # 已拍板但下游未传播（崩溃恢复；任务 10 接线）。终态决议必须属于
        # 最新轮——驳回（rejected 也是终态）后产生新轮次时，旧决议是历史，
        # 仍须按最新轮摘要正常评估是否出下一版草案（FR-21b）。
        return "propagate"
    summary = session.scalar(
        select(RoundSummary).where(RoundSummary.round_id == rnd.id,
                                   RoundSummary.generation_status == "ok")
    )
    if summary is None:
        return "done"
    if latest_res is not None and latest_res.status == RESOLUTION_STATUS_PENDING_REVIEW:
        return "draft"  # 草案已存在（重驱动）：draft 相位幂等跳过并路由
    if summary.convergence in (CONVERGENCE_PROVISIONALLY_READY,
                               CONVERGENCE_CONVERGED):
        return "draft"
    return "done"


def build_matter_graph(*, session_factory, settings: Settings, llm,
                       checkpointer):
    """Compile the matter graph. Nodes close over session_factory/settings/llm."""

    def node_generate_round(state: MatterGraphState) -> dict:
        with session_factory() as session:
            rnd = _latest_round(session, state["matter_id"])
            if rnd is not None and rnd.status == "generating" and not rnd.questions:
                _generate_first_round_phase(session, rnd, llm)
            session.commit()
        return {}

    def node_summarize(state: MatterGraphState) -> dict:
        with session_factory() as session:
            rnd = _latest_round(session, state["matter_id"])
            if rnd is not None and rnd.status == "awaiting_summary":
                _summarize_phase(session, rnd, llm)
            session.commit()
        return {}

    def node_branch(state: MatterGraphState) -> dict:
        with session_factory() as session:
            matter = session.get(Matter, state["matter_id"])
            rnd = _latest_round(session, state["matter_id"])
            if rnd is not None:
                _branch_phase(session, rnd, llm)
            session.flush()
            session.refresh(matter)
            route = _compute_branch_route(session, matter)
            session.commit()
        return {"branch": route}

    def node_draft_resolution(state: MatterGraphState) -> dict:
        with session_factory() as session:
            matter = session.get(Matter, state["matter_id"])
            _draft_resolution_phase(session, matter, llm)
            session.commit()
        # 任务 10 在此按收敛态路由到闸门；本任务图到 END。
        return {"after_draft": "end"}

    graph = StateGraph(MatterGraphState)
    graph.add_node(NODE_GENERATE_ROUND, node_generate_round)
    graph.add_node(NODE_SUMMARIZE, node_summarize)
    graph.add_node(NODE_BRANCH, node_branch)
    graph.add_node(NODE_DRAFT_RESOLUTION, node_draft_resolution)
    graph.add_edge(START, NODE_GENERATE_ROUND)
    graph.add_edge(NODE_GENERATE_ROUND, NODE_SUMMARIZE)
    graph.add_edge(NODE_SUMMARIZE, NODE_BRANCH)
    graph.add_conditional_edges(
        NODE_BRANCH,
        lambda state: state["branch"],
        {"draft": NODE_DRAFT_RESOLUTION, "propagate": END, "done": END},
    )
    graph.add_edge(NODE_DRAFT_RESOLUTION, END)
    return graph.compile(checkpointer=checkpointer)


def _pending_interrupts(snapshot) -> list:
    return [i for task in snapshot.tasks for i in task.interrupts]


def open_checkpointer(path: str) -> SqliteSaver:
    """Self-built connection + SqliteSaver. Do NOT use
    SqliteSaver.from_conn_string: it only does
    sqlite3.connect(path, check_same_thread=False) and never sets
    busy_timeout (a per-connection PRAGMA; WAL is file-level and
    unaffected), which PRD 10.3 requires. Caller owns the connection
    and must close it (saver.conn.close())."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def drive_matter_tick(session_factory, settings: Settings, *, matter_id: str,
                      llm) -> None:
    """One drive event for a matter thread. See module docstring for the
    three-way checkpoint dispatch."""
    path = sqlite_path_from_url(settings.database_url)
    saver = open_checkpointer(path)
    try:
        graph = build_matter_graph(
            session_factory=session_factory, settings=settings, llm=llm,
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": matter_id}}
        snapshot = graph.get_state(config)
        if snapshot.next:
            if _pending_interrupts(snapshot):
                logger.info("matter %s paused at %s; tick skipped",
                            matter_id, snapshot.next)
                return
            logger.info("matter %s crashed between nodes; continuing",
                        matter_id)
            graph.invoke(None, config)
            return
        graph.invoke({"matter_id": matter_id}, config)
    finally:
        saver.conn.close()
