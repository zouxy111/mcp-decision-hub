"""MCP 工具注册面：`tools.py` 里注册的工具必须与对外承诺的工具清单一一对应。

为什么需要这条：`mcp_get_digest` 曾经**只有 HTTP 出口**——`methods.py` 里实现
完了、`routes_api.py` 也挂上了，但 `tools.py` 没注册，MCP 侧根本调不到；而且
没有任何既有测试会因此变红。缺口不是被测出来的，是被人工核对碰上的。本文件
把「注册面」本身变成断言对象。
"""

import pytest
from fastmcp import FastMCP

from hub.mcp_server.tools import register_tools

# 任务流水线（r5Am9i D1–D6 的前半）
PIPELINE_TOOLS = {
    "list_pending_tasks", "get_task", "submit_output", "get_matter_status",
}

# 立场层 7 个中**已落地**的 6 个（第 7 个 ask_participant 见下方 xfail）
LANDED_STANCE_TOOLS = {
    "declare_item", "submit_stance", "read_stance",
    "get_summary", "decide_item", "get_digest",
}


def _registered_names(settings) -> set[str]:
    mcp = FastMCP("registry-probe")
    register_tools(mcp, lambda: None, settings)
    return set(mcp._tool_manager._tools)


def test_流水线四个工具全部注册(settings):
    """对照：既有注册面不能被后来者碰坏。"""
    assert PIPELINE_TOOLS <= _registered_names(settings)


def test_立场层已落地六个工具全部注册(settings):
    names = _registered_names(settings)
    missing = LANDED_STANCE_TOOLS - names
    assert not missing, f"MCP 侧缺失工具: {sorted(missing)}"


@pytest.mark.xfail(
    strict=True,
    reason="ask_participant 未落地：PRD-01 要求把定向问题挂到「目标参与人"
           "本轮 questions_for」上，但 questions_for 是 stances 表的列（按 "
           "(matter, round, user) 唯一）。目标本轮尚未提交立场时无处可挂，"
           "而 PRD-01 的「文件所有权」未含 migrations.py —— 缺一张待答问题表"
           "的口径。见 outputs/2026-09-16-r5Am9i-ask_participant-阻塞.md",
)
def test_ask_participant已注册为MCP工具(settings):
    """本用例现在必须失败；口径定下并实现后，xpass 会让本行变红，强制翻转。"""
    assert "ask_participant" in _registered_names(settings)
