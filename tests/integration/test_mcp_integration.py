"""Process-level MCP integration tests: real uvicorn + fastmcp Client."""

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
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select, update

from hub.api import matters as matter_svc
from hub.api.tokens import issue_token
from hub.db.models import AuditEvent, Output, Task
from hub.db.session import init_db, make_engine, make_session_factory
from hub.domain.digest import compute_content_digest
from hub.domain.timeutil import iso_z, utcnow
from tests.conftest import make_user

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db_url = f"sqlite:///{tmp_path_factory.mktemp('hub')}/it.db"
    engine = make_engine(db_url)
    init_db(engine)
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "it-secret",
        "LLM_PROVIDER_NAME": "it-provider",
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
    yield SimpleNamespace(base=base, db_url=db_url)
    proc.terminate()
    proc.wait(timeout=10)


def _scenario(server, suffix):
    """Fresh users/matter/tasks per test, written directly to the shared DB file."""
    engine = make_engine(server.db_url)
    factory = make_session_factory(engine)
    with factory() as s:
        init = make_user(s, f"init_{suffix}", password="pw-123456")
        alice = make_user(s, f"alice_{suffix}", password="pw-123456")
        bob = make_user(s, f"bob_{suffix}", password="pw-123456")
        s.flush()
        _, token_a = issue_token(s, user=alice, name="a")
        _, token_i = issue_token(s, user=init, name="i")
        matter = matter_svc.create_matter(
            s, initiator=init, title=f"T-{suffix}", goal="G", background="B",
            participant_ids=[alice.id, bob.id], initiator_participates=False,
            timeout_seconds=3600, max_rounds=10, draft_questions=["Q1?", "Q2?"],
        )
        matter_svc.start_matter(s, matter_id=matter.id, actor=init)
        s.commit()
        task_a = s.scalar(select(Task).where(Task.assignee_id == alice.id,
                                             Task.matter_id == matter.id))
        task_b = s.scalar(select(Task).where(Task.assignee_id == bob.id,
                                             Task.matter_id == matter.id))
        return SimpleNamespace(
            matter_id=matter.id, task_a_id=task_a.id, task_b_id=task_b.id,
            token_a=token_a, token_i=token_i, alice_id=alice.id,
        )


def _client(server, token) -> Client:
    transport = StreamableHttpTransport(
        url=f"{server.base}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
    )
    return Client(transport)


def _call(server, token, tool, args):
    async def run():
        async with _client(server, token) as c:
            result = await c.call_tool(tool, args)
            return result.data

    return asyncio.run(run())


def _call_error(server, token, tool, args) -> str:
    async def run():
        async with _client(server, token) as c:
            await c.call_tool(tool, args)

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(run())
    return str(exc_info.value)


def _submit_args(task_id, answers=None, notes="n", **overrides):
    answers = answers if answers is not None else [
        {"question_id": "q1", "content": "回答一"},
        {"question_id": "q2", "content": "回答二"},
    ]
    args = {
        "task_id": task_id,
        "answers": answers,
        "notes": notes,
        "human_approved": True,
        "approved_at": iso_z(utcnow()),
        "content_digest": compute_content_digest(answers, notes),
        "idempotency_key": str(uuid.uuid4()),
    }
    args.update(overrides)
    return args


def test_token_isolation_list_and_get(server):
    sc = _scenario(server, "iso")
    mine = _call(server, sc.token_a, "list_pending_tasks", {})
    assert [t["task_id"] for t in mine["tasks"]] == [sc.task_a_id]
    assert mine["next_poll_after"].endswith("Z")
    # A's token pulling B's task -> FORBIDDEN_SCOPE (403 semantics)
    err = _call_error(server, sc.token_a, "get_task", {"task_id": sc.task_b_id})
    assert "FORBIDDEN_SCOPE" in err


def test_get_task_context_has_no_other_answers(server):
    sc = _scenario(server, "ctx")
    result = _call(server, sc.token_a, "get_task", {"task_id": sc.task_a_id})
    assert result["matter"]["title"] == "T-ctx"
    assert result["previous_summary"] is None
    assert result["llm_provider"] == "it-provider"
    assert [q["question_id"] for q in result["round"]["questions"]] == ["q1", "q2"]


def test_submit_missing_human_approved_422(server):
    sc = _scenario(server, "noapprove")
    args = _submit_args(sc.task_a_id, human_approved=False)
    err = _call_error(server, sc.token_a, "submit_output", args)
    assert "HUMAN_APPROVAL_REQUIRED" in err


def test_submit_digest_mismatch_422(server):
    sc = _scenario(server, "badigest")
    args = _submit_args(sc.task_a_id, content_digest="0" * 64)
    err = _call_error(server, sc.token_a, "submit_output", args)
    assert "HUMAN_APPROVAL_REQUIRED" in err


def test_submit_content_limit_422(server):
    sc = _scenario(server, "limit")
    args = _submit_args(
        sc.task_a_id,
        answers=[{"question_id": "q1", "content": "a" * (16 * 1024 + 1)}],
    )
    err = _call_error(server, sc.token_a, "submit_output", args)
    assert "CONTENT_LIMIT_EXCEEDED" in err
    assert "answers[].content" in err


def test_non_participating_initiator_status_200(server):
    sc = _scenario(server, "init200")
    result = _call(server, sc.token_i, "get_matter_status",
                   {"matter_id": sc.matter_id})
    assert result["status"] == "collecting"
    assert result["rounds_total"] == 1
    assert result["recent_rounds"][0]["summaries"] == []
    names = {p["username"] for p in result["participant_progress"]}
    assert names == {"alice_init200", "bob_init200"}


def test_idempotency_six_quadrants(server):
    sc = _scenario(server, "six")
    # row 5: new key + pending -> create
    args = _submit_args(sc.task_a_id)
    first = _call(server, sc.token_a, "submit_output", args)
    assert first["status"] == "submitted"
    # row 1: same key + same body -> replay first response
    replay = _call(server, sc.token_a, "submit_output", dict(args))
    assert replay == first
    # row 2: same key + different body -> 409 IDEMPOTENCY_CONFLICT
    conflict_args = _submit_args(
        sc.task_a_id,
        answers=[{"question_id": "q1", "content": "改了"}],
        idempotency_key=args["idempotency_key"],
    )
    err = _call_error(server, sc.token_a, "submit_output", conflict_args)
    assert "IDEMPOTENCY_CONFLICT" in err
    # row 3: new key + identical content -> 200 replay + audit
    same_content = _submit_args(sc.task_a_id)
    second = _call(server, sc.token_a, "submit_output", same_content)
    assert second["submitted_at"] == first["submitted_at"]
    # row 4: new key + different content -> 409 TASK_ALREADY_SUBMITTED
    diff_args = _submit_args(
        sc.task_a_id, answers=[{"question_id": "q1", "content": "不同"}]
    )
    err = _call_error(server, sc.token_a, "submit_output", diff_args)
    assert "TASK_ALREADY_SUBMITTED" in err
    # row 6: timeout task -> 409 INVALID_STATE_TRANSITION (direct DB write)
    engine = make_engine(server.db_url)
    factory = make_session_factory(engine)
    with factory() as s:
        s.execute(
            update(Task).where(Task.id == sc.task_a_id).values(status="timeout")
        )
        s.commit()
    err = _call_error(server, sc.token_a, "submit_output",
                      _submit_args(sc.task_a_id))
    assert "INVALID_STATE_TRANSITION" in err
    # exactly one Output for task_a despite all the traffic
    with factory() as s:
        count = s.scalar(
            select(func.count()).select_from(Output).where(
                Output.task_id == sc.task_a_id)
        )
        replayed = s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "output_replayed")
        ).all()
    assert count == 1
    assert len([r for r in replayed]) >= 1


def test_revoked_token_401(server):
    sc = _scenario(server, "revoke")
    engine = make_engine(server.db_url)
    factory = make_session_factory(engine)
    with factory() as s:
        from hub.api.tokens import revoke_token
        from hub.db.models import AgentToken, User

        alice = s.get(User, sc.alice_id)
        token_row = s.scalar(
            select(AgentToken).where(AgentToken.user_id == alice.id)
        )
        revoke_token(s, user=alice, token_id=token_row.id)
        s.commit()
    resp = httpx.post(
        f"{server.base}/mcp/",
        headers={"Authorization": f"Bearer {sc.token_a}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_INVALID_TOKEN"
