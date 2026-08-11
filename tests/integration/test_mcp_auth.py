"""Auth middleware verification against a real uvicorn process."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub.api.tokens import issue_token
from hub.db.session import init_db, make_engine, make_session_factory
from tests.conftest import make_user

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    db_url = f"sqlite:///{tmp_path}/it.db"
    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        user = make_user(session, "alice", password="pw-123456")
        _, plaintext = issue_token(session, user=user, name="it-agent")
        session.commit()
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "it-secret",
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hub.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/login", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("server did not become ready")
    yield type("Srv", (), {"base": base, "token": plaintext})()
    proc.terminate()
    proc.wait(timeout=10)


def test_mcp_endpoint_rejects_missing_token(server):
    resp = httpx.post(f"{server.base}/mcp/", json={"jsonrpc": "2.0", "id": 1,
                                                   "method": "initialize",
                                                   "params": {}})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


def test_mcp_endpoint_rejects_garbage_token(server):
    resp = httpx.post(
        f"{server.base}/mcp/",
        headers={"Authorization": "Bearer hdt_garbage"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"


def test_mcp_endpoint_accepts_valid_token(server):
    resp = httpx.post(
        f"{server.base}/mcp/",
        headers={
            "Authorization": f"Bearer {server.token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "it", "version": "0"}},
        },
    )
    assert resp.status_code == 200
