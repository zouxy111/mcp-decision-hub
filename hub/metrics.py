"""Prometheus 指标暴露（PRD P2 指标看板）。

零依赖实现：手写 text exposition format，不引入 prometheus-client。

指标清单：
- ``hub_http_requests_total{method,route,status}`` counter — ASGI middleware 全量埋点
- ``hub_http_request_duration_seconds_sum/_count{route}`` counter — 简化直方图（均值=sum/count）
- ``hub_rate_limited_total{dimension}`` counter — 限流拒绝（mcp_token/mcp_account/mcp_submit/login_username/login_ip）
- ``hub_scheduler_scans_total`` counter — 超时调度器扫描次数
- ``hub_tasks_timeout_total`` counter — 判定超时的任务数
- ``hub_queue_depth{queue}`` gauge — drive/resume/board 队列深度（采集时读）
- ``hub_matters_total{status}`` gauge — 事项按状态计数（采集时 DB count）

/metrics 端点保护：生产环境 uvicorn 直接对外（0.0.0.0:8000），不能裸奔。
规则：配置了 ``METRICS_TOKEN`` 环境变量时要求 ``Authorization: Bearer <token>``；
未配置时仅允许本机直连（Host 为 localhost/127.0.0.1 且不带 X-Forwarded-For），
经 Caddy 反代或公网访问一律 404（不暴露端点存在性）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class MetricsRegistry:
    """线程安全的指标注册表。Counter 只增，Gauge 采集时回调取值。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._gauges: dict[str, Callable[[], dict[tuple[tuple[str, str], ...], float]]] = {}
        self._help: dict[str, str] = {}
        self._types: dict[str, str] = {}

    def register_counter(self, name: str, help_text: str) -> None:
        self._counters.setdefault(name, {})
        self._help[name] = help_text
        self._types[name] = "counter"

    def register_gauge(self, name: str, help_text: str,
                       collector: Callable[[], dict[tuple[tuple[str, str], ...], float]]) -> None:
        self._gauges[name] = collector
        self._help[name] = help_text
        self._types[name] = "gauge"

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + amount

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            snapshot = {n: dict(s) for n, s in self._counters.items()}
        for name in sorted(set(snapshot) | set(self._gauges)):
            lines.append(f"# HELP {name} {self._help.get(name, '')}")
            lines.append(f"# TYPE {name} {self._types.get(name, 'counter')}")
            if name in snapshot:
                for labels, value in sorted(snapshot[name].items()):
                    lines.append(_format_sample(name, labels, value))
            elif name in self._gauges:
                try:
                    for labels, value in sorted(self._gauges[name]().items()):
                        lines.append(_format_sample(name, labels, value))
                except Exception:
                    # 采集器失败（如 DB 不可用）不拖垮整个端点：输出空序列
                    pass
        lines.append("")
        return "\n".join(lines)


def _format_sample(name: str, labels: tuple[tuple[str, str], ...], value: float) -> str:
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels)
        return f"{name}{{{label_str}}} {value:g}"
    return f"{name} {value:g}"


# 全局注册表（进程内单例；测试间隔离用 reset_registry）
registry = MetricsRegistry()


def reset_registry() -> None:
    """测试用：清空并重注册全部指标。"""
    global registry
    registry = MetricsRegistry()
    _register_all(registry)


def _route_label(scope: Scope) -> str:
    """从 scope 提取路由模板作标签，控制基数。

    静态文件 / MCP 挂载折叠为挂载前缀，避免 path 爆序列。
    """
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path  # type: ignore[return-value]
    path: str = scope.get("path", "")
    for prefix in ("/static", "/mcp"):
        if path.startswith(prefix):
            return prefix
    return "unmatched"


class MetricsMiddleware:
    """纯 ASGI middleware：请求计数 + 耗时（sum/count）。"""

    def __init__(self, app: ASGIApp, reg: MetricsRegistry) -> None:
        self.app = app
        self.reg = reg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_label(scope)
            method = scope.get("method", "GET")
            self.reg.inc("hub_http_requests_total",
                         method=method, route=route,
                         status=str(status_holder["status"]))
            self.reg.inc("hub_http_request_duration_seconds_sum",
                         elapsed, route=route)
            self.reg.inc("hub_http_request_duration_seconds_count",
                         route=route)


def rate_limited(dimension: str) -> None:
    """限流拒绝埋点。dimension: mcp_token/mcp_account/mcp_submit/login_username/login_ip"""
    registry.inc("hub_rate_limited_total", dimension=dimension)


def scheduler_scan() -> None:
    registry.inc("hub_scheduler_scans_total")


def task_timeout(count: int = 1) -> None:
    registry.inc("hub_tasks_timeout_total", float(count))


def _metrics_allowed(request: Request) -> bool:
    """/metrics 访问控制：token 优先，否则仅本机直连。

    生产环境 uvicorn 0.0.0.0:8000 直接对外，经 Caddy 或公网到达的请求
    必须 404（不暴露端点存在性）。
    """
    token = os.environ.get("METRICS_TOKEN") or getattr(
        request.app.state, "settings", None) and getattr(
        request.app.state.settings, "metrics_token", None)
    if token:
        auth = request.headers.get("authorization", "")
        return auth == f"Bearer {token}"
    host = request.headers.get("host", "").split(":")[0]
    if host not in ("localhost", "127.0.0.1"):
        return False
    if "x-forwarded-for" in request.headers:
        return False
    return True


def make_metrics_endpoint():
    def collect_queues(app) -> dict[tuple[tuple[str, str], ...], float]:
        out: dict[tuple[tuple[str, str], ...], float] = {}
        for name, attr in (("drive", "drive_queue"),
                           ("resume", "resume_queue"),
                           ("board", "board_queue")):
            queue = getattr(app.state, attr, None)
            if queue is not None:
                out[(("queue", name),)] = float(queue.qsize())
        return out

    async def metrics_endpoint(request: Request):
        if not _metrics_allowed(request):
            return PlainTextResponse("Not Found", status_code=404)
        # 队列深度 gauge：每次请求刷新注册（collector 持最新 app 引用，
        # reset_registry 后也能自愈）
        registry.register_gauge(
            "hub_queue_depth", "后台队列深度（采集时读取）",
            lambda: collect_queues(request.app))
        return PlainTextResponse(registry.render(),
                                 media_type="text/plain; version=0.0.4")
    return metrics_endpoint


def _register_all(reg: MetricsRegistry) -> None:
    reg.register_counter("hub_http_requests_total", "HTTP 请求总数（按方法/路由/状态码）")
    reg.register_counter("hub_http_request_duration_seconds_sum", "请求耗时总和（秒）")
    reg.register_counter("hub_http_request_duration_seconds_count", "请求耗时样本数")
    reg.register_counter("hub_rate_limited_total", "限流拒绝总数（按维度）")
    reg.register_counter("hub_scheduler_scans_total", "超时调度器扫描次数")
    reg.register_counter("hub_tasks_timeout_total", "判定超时的任务总数")


_register_all(registry)
