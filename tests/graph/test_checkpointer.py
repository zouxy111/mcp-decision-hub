"""Lock the verified LangGraph 1.2.11 semantics that M3 relies on.

If any of these fail after a dependency upgrade, re-read the "LangGraph
技术验证结论" section of the M3 plan and re-verify before touching graph code.
"""

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from hub.db.session import init_db, make_engine


class _State(TypedDict, total=False):
    n: int
    route: str
    done: str


def _saver(path):
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")  # 连接级属性，逐连接设置（PRD 10.3）
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def test_checkpointer_shares_business_sqlite_file(settings, tmp_path):
    """PRD 10.3: checkpoint tables live in the same SQLite file as business
    tables."""
    engine = make_engine(settings.database_url)
    init_db(engine)
    db_path = str(tmp_path / "test.db")
    saver = _saver(db_path)
    names = {
        r[0]
        for r in saver.conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    assert {"matters", "rounds", "tasks"} <= names  # business tables intact
    assert {"checkpoints", "writes"} <= names  # checkpoint tables added


def _gate_graph(saver):
    def node_a(state):
        return {"n": (state.get("n") or 0) + 1}

    def gate(state):
        payload = interrupt({"wait": "decision"})
        return {"done": payload["decision"]}

    g = StateGraph(_State)
    g.add_node("node_a", node_a)
    g.add_node("gate", gate)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=saver)


def test_interrupt_pauses_and_resume_returns_payload(tmp_path):
    graph = _gate_graph(_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    result = graph.invoke({"n": 0}, config)
    assert "__interrupt__" in result
    assert graph.get_state(config).next == ("gate",)
    result = graph.invoke(Command(resume={"decision": "approved"}), config)
    assert result["done"] == "approved"
    assert graph.get_state(config).next == ()


def test_plain_invoke_while_paused_restarts_from_start(tmp_path):
    """FOOTGUN locked: plain input on an interrupted thread re-runs from
    START — and the input OVERWRITES checkpointed channel values (state
    pollution, locked below). tick code must never do this — check
    get_state().next first."""
    graph = _gate_graph(_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    graph.invoke({"n": 0}, config)
    result = graph.invoke({"n": 0}, config)  # 错误用法，此处仅锁定语义
    # node_a 重跑了，但输入 {"n": 0} 会覆盖 state 通道值（状态污染坑，一并
    # 锁定）：通道被重置为 0 再自增，结果是 1 而不是 2
    assert result["n"] == 1
    assert graph.get_state(config).next == ("gate",)  # gate re-interrupted


def test_invoke_none_while_paused_reinterrupts_safely(tmp_path):
    graph = _gate_graph(_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    graph.invoke({"n": 0}, config)
    result = graph.invoke(None, config)
    assert "__interrupt__" in result
    assert result["n"] == 1  # node_a did NOT re-run
    assert graph.get_state(config).next == ("gate",)


def test_resume_when_not_paused_is_silent_noop(tmp_path):
    """FOOTGUN locked: Command(resume=...) on a non-paused thread does nothing
    and does not raise. resume code must check the pending node first."""
    graph = _gate_graph(_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    graph.invoke({"n": 0}, config)
    graph.invoke(Command(resume={"decision": "approved"}), config)
    assert graph.get_state(config).next == ()
    result = graph.invoke(Command(resume={"decision": "ignored"}), config)
    assert result["done"] == "approved"  # unchanged


def test_crash_pending_state_continues_with_none_input(tmp_path):
    """Crash between nodes: invoke(None) continues pending nodes; invoke with
    plain input would restart from START (locked above)."""
    log = []

    def a(state):
        log.append("a")
        return {"n": (state.get("n") or 0) + 1}

    def b(state):
        log.append("b")
        return {"n": state["n"] + 10}

    g = StateGraph(_State)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    graph = g.compile(checkpointer=_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    graph.update_state(config, {"n": 1}, as_node="a")  # simulate crash after a
    assert graph.get_state(config).next == ("b",)
    result = graph.invoke(None, config)
    assert log == ["b"]  # only the pending node ran
    assert result["n"] == 11


def test_sequential_gates_pause_in_order(tmp_path):
    """provisional_gate → resume accept → decision_gate pauses again — the
    exact two-gate topology of the resolution gate."""

    def draft(state):
        return {}

    def provisional_gate(state):
        payload = interrupt({"stage": "provisional"})
        return {"route": payload["action"]}

    def decision_gate(state):
        payload = interrupt({"stage": "decision"})
        return {"done": payload["decision"]}

    def archive(state):
        # NOTE: must not write "done" — it would overwrite the gate's result.
        return {"route": "archived"}

    g = StateGraph(_State)
    g.add_node("draft", draft)
    g.add_node("provisional_gate", provisional_gate)
    g.add_node("decision_gate", decision_gate)
    g.add_node("archive", archive)
    g.add_edge(START, "draft")
    g.add_edge("draft", "provisional_gate")
    g.add_conditional_edges(
        "provisional_gate", lambda s: s["route"], {"accept": "decision_gate"}
    )
    g.add_edge("decision_gate", "archive")
    g.add_edge("archive", END)
    graph = g.compile(checkpointer=_saver(tmp_path / "g.db"))
    config = {"configurable": {"thread_id": "m1"}}
    graph.invoke({}, config)
    assert graph.get_state(config).next == ("provisional_gate",)
    graph.invoke(Command(resume={"action": "accept"}), config)
    assert graph.get_state(config).next == ("decision_gate",)
    result = graph.invoke(Command(resume={"decision": "approved"}), config)
    assert result["done"] == "approved"
    assert result["route"] == "archived"  # archive ran after decision_gate
    assert graph.get_state(config).next == ()
