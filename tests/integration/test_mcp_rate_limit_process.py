"""Process-level rate-limit regression tests: real uvicorn, small thresholds.

Covers the M4 任务 11 smoke scenario that exposed the token-key bug
(token dimension must count per AgentToken.id, not per user id):
two tokens of the same account must have independent token buckets,
while the account bucket is shared.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import Task
from hub.db.session import init_db, make_engine, make_session_factory
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from tests.conftest import make_user

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(db_url: str, env_extra: dict):
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "rl-secret",
        "LLM_PROVIDER_NAME": "rl-provider",
        "PYTHONPATH": str(PROJECT_ROOT),
        **env_extra,
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
                return proc, base
        except httpx.TransportError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("server did not become ready")


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _mcp_post(base, token):
    return httpx.post(
        f"{base}/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "rl-test", "version": "0"}}},
        timeout=5,
    )


@pytest.fixture()
def rl_env(tmp_path):
    """Users/tokens/matter in a fresh DB; caller starts servers as needed."""
    db_url = f"sqlite:///{tmp_path}/rl.db"
    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        carol = make_user(s, "carol_rl")
        dave = make_user(s, "dave_rl")
        alice = make_user(s, "alice_rl")
        bob = make_user(s, "bob_rl")
        init = make_user(s, "init_rl")
        s.flush()
        _, token_carol = issue_token(s, user=carol, name="c")
        _, token_dave1 = issue_token(s, user=dave, name="d1")
        _, token_dave2 = issue_token(s, user=dave, name="d2")
        _, token_alice = issue_token(s, user=alice, name="a")
        matter = matter_svc.create_matter(
            s, initiator=init, title="T-rl", goal="G", background="B",
            participant_ids=[alice.id, bob.id], initiator_participates=False,
            timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?", "Q2?"])
        matter_svc.start_matter(s, matter_id=matter.id, actor=init)
        s.commit()
        task_alice_id = s.scalar(select(Task.id).where(
            Task.assignee_id == alice.id, Task.matter_id == matter.id))
    return SimpleNamespace(
        db_url=db_url, token_carol=token_carol, token_dave1=token_dave1,
        token_dave2=token_dave2, token_alice=token_alice,
        task_alice_id=task_alice_id,
    )


def test_token_dimension_429_with_retry_after(rl_env):
    proc, base = _start_server(rl_env.db_url, {
        "RATE_LIMIT_TOKEN_PER_MINUTE": "3",
        "RATE_LIMIT_ACCOUNT_PER_MINUTE": "100",
        "RATE_LIMIT_SUBMIT_PER_MINUTE": "100",
    })
    try:
        statuses = [_mcp_post(base, rl_env.token_carol).status_code
                    for _ in range(4)]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429
        resp = _mcp_post(base, rl_env.token_carol)
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1
        assert resp.json()["error_code"] == "RATE_LIMITED"
    finally:
        _stop(proc)


def test_two_tokens_same_account_independent_token_buckets(rl_env):
    """Regression: token dimension keys on AgentToken.id, not user.id."""
    proc, base = _start_server(rl_env.db_url, {
        "RATE_LIMIT_TOKEN_PER_MINUTE": "3",
        "RATE_LIMIT_ACCOUNT_PER_MINUTE": "100",
        "RATE_LIMIT_SUBMIT_PER_MINUTE": "100",
    })
    try:
        st1 = [_mcp_post(base, rl_env.token_dave1).status_code for _ in range(4)]
        st2 = [_mcp_post(base, rl_env.token_dave2).status_code for _ in range(4)]
        # token 1 hits its own cap at the 4th request
        assert st1 == [200, 200, 200, 429]
        # token 2 must NOT be affected by token 1's usage
        assert st2 == [200, 200, 200, 429]
    finally:
        _stop(proc)


def test_account_dimension_shared_across_tokens(rl_env):
    proc, base = _start_server(rl_env.db_url, {
        "RATE_LIMIT_TOKEN_PER_MINUTE": "10",
        "RATE_LIMIT_ACCOUNT_PER_MINUTE": "5",
        "RATE_LIMIT_SUBMIT_PER_MINUTE": "100",
    })
    try:
        st1 = [_mcp_post(base, rl_env.token_dave1).status_code for _ in range(3)]
        st2 = [_mcp_post(base, rl_env.token_dave2).status_code for _ in range(3)]
        assert st1 == [200, 200, 200]
        # account cap 5: dave1 used 3, dave2 gets 2 more, 6th request 429
        assert st2 == [200, 200, 429]
    finally:
        _stop(proc)


def test_submit_dimension_tool_error(rl_env):
    proc, base = _start_server(rl_env.db_url, {
        "RATE_LIMIT_TOKEN_PER_MINUTE": "100",
        "RATE_LIMIT_ACCOUNT_PER_MINUTE": "200",
        "RATE_LIMIT_SUBMIT_PER_MINUTE": "2",
    })
    try:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        def submit_args():
            answers = [{"question_id": "q1", "content": "回答一"},
                       {"question_id": "q2", "content": "回答二"}]
            notes = "rl"
            return {
                "task_id": rl_env.task_alice_id, "answers": answers,
                "notes": notes, "human_approved": True,
                "approved_at": iso_z(utcnow()),
                "content_digest": compute_content_digest(answers, notes),
                "idempotency_key": str(uuid.uuid4()),
            }

        async def run():
            transport = StreamableHttpTransport(
                url=f"{base}/mcp/",
                headers={"Authorization": f"Bearer {rl_env.token_alice}"})
            outcomes = []
            async with Client(transport) as c:
                for _ in range(3):
                    try:
                        await c.call_tool("submit_output", submit_args())
                        outcomes.append("ok")
                    except Exception as e:
                        outcomes.append(str(e))
            return outcomes

        outcomes = asyncio.run(run())
        assert outcomes[0] == "ok"
        assert outcomes[1] == "ok"
        assert "RATE_LIMITED" in outcomes[2]
        assert "retry_after" in outcomes[2]
    finally:
        _stop(proc)
