"""MCP 工具注册面：`tools.py` 里注册的工具必须与对外承诺的工具清单一一对应。

为什么需要这条：`mcp_get_digest` 曾经**只有 HTTP 出口**——`methods.py` 里实现
完了、`routes_api.py` 也挂上了，但 `tools.py` 没注册，MCP 侧根本调不到；而且
没有任何既有测试会因此变红。缺口不是被测出来的，是被人工核对碰上的。本文件
把「注册面」本身变成断言对象。
"""

from fastmcp import FastMCP

from hub.mcp_server.tools import register_tools

# 任务流水线（r5Am9i D1–D6 的前半）
PIPELINE_TOOLS = {
    "list_pending_tasks", "get_task", "submit_output", "get_matter_status",
}

# 立场层 7 个工具
STANCE_TOOLS = {
    "declare_item", "submit_stance", "read_stance",
    "get_summary", "ask_participant", "decide_item", "get_digest",
}


def _registered_names(settings) -> set[str]:
    mcp = FastMCP("registry-probe")
    register_tools(mcp, lambda: None, settings)
    return set(mcp._tool_manager._tools)


def test_流水线四个工具全部注册(settings):
    """对照：既有注册面不能被后来者碰坏。"""
    assert PIPELINE_TOOLS <= _registered_names(settings)


def test_立场层七个工具全部注册(settings):
    names = _registered_names(settings)
    missing = STANCE_TOOLS - names
    assert not missing, f"MCP 侧缺失工具: {sorted(missing)}"
