"""Prometheus /metrics 端点与埋点测试。

覆盖：本机可访问、外网 404、XFF 拒绝、请求计数/耗时埋点、
login 限流埋点、队列深度 gauge、text exposition 格式。
"""

from hub import metrics

import pytest


@pytest.fixture(autouse=True)
def _fresh_registry():
    """registry 是进程级单例：每个测试重置，避免计数跨用例累积。"""
    metrics.reset_registry()
    yield


def _get_metrics(client, **headers):
    merged = {"host": "localhost"}
    merged.update(headers)
    return client.get("/metrics", headers=merged)


def test_metrics_endpoint_local_ok(client):
    resp = _get_metrics(client)
    assert resp.status_code == 200
    body = resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# HELP hub_http_requests_total" in body
    assert "# TYPE hub_http_requests_total counter" in body


def test_metrics_endpoint_rejects_external_host(client):
    resp = client.get("/metrics", headers={"host": "evil.example.com"})
    assert resp.status_code == 404


def test_metrics_endpoint_rejects_forwarded(client):
    resp = client.get(
        "/metrics",
        headers={"host": "localhost", "x-forwarded-for": "1.2.3.4"},
    )
    assert resp.status_code == 404


def test_http_request_counter_incremented(client):
    before = _get_metrics(client).text
    client.get("/login")
    client.get("/login")
    after = _get_metrics(client).text
    assert 'route="/login"' in after
    # /metrics 自身不计入（middleware 跳过），/login 至少 +2
    line = [ln for ln in after.splitlines()
            if ln.startswith("hub_http_requests_total{")
            and 'route="/login"' in ln and 'status="200"' in ln]
    assert line, f"未找到 /login 计数序列:\n{after}"
    assert line[0].endswith(" 2")


def test_duration_sum_and_count_present(client):
    client.get("/login")
    body = _get_metrics(client).text
    assert 'hub_http_request_duration_seconds_sum{route="/login"}' in body
    assert 'hub_http_request_duration_seconds_count{route="/login"} 1' in body


def test_queue_depth_gauge_present(client):
    body = _get_metrics(client).text
    assert 'hub_queue_depth{queue="drive"}' in body
    assert 'hub_queue_depth{queue="resume"}' in body


def test_login_rate_limit_metric(client, db_session, settings):
    from tests.conftest import make_user

    make_user(db_session, "ratelimit_user", password="right-pw")
    db_session.commit()
    # username 维度默认 5/min：第 6 次错误登录触发 429
    for _ in range(6):
        client.post("/login", data={"username": "ratelimit_user",
                                    "password": "wrong-pw"})
    body = _get_metrics(client).text
    line = [ln for ln in body.splitlines()
            if ln.startswith("hub_rate_limited_total{")
            and 'dimension="login_username"' in ln]
    assert line, f"未找到 login_username 限流序列:\n{body}"
    assert not line[0].endswith(" 0")


def test_metrics_endpoint_not_counted(client):
    """/metrics 自身请求不应产生 http_requests_total 序列（避免自反馈）。"""
    _get_metrics(client)
    body = _get_metrics(client).text
    assert 'route="/metrics"' not in body


def test_registry_reset():
    metrics.reset_registry()
    body = metrics.registry.render()
    assert "# TYPE hub_http_requests_total counter" in body
    assert "hub_rate_limited_total" in body
