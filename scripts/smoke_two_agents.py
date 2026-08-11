# scripts/smoke_two_agents.py
"""Two-agent smoke loop against a running server (real DeepSeek).

Prereq:
  1. .env contains DEEPSEEK_API_KEY (and default DATABASE_URL)
  2. uv run python scripts/smoke_seed.py  (note the printed tokens)
  3. uv run uvicorn hub.main:app --port 8765
  4. Via browser: login smoke_init / smoke-pw-123, create a matter with
     smoke_alice + smoke_bob as participants and 1-2 first-round questions,
     then click 开始.

Usage:
  SMOKE_TOKEN_ALICE=... SMOKE_TOKEN_BOB=... uv run python scripts/smoke_two_agents.py

Expected: both agents pull tasks and submit; the platform summarizes round 1,
judges convergence, and either opens round 2 (continue) or moves to
awaiting_decision / blocked. The script polls get_matter_status until the
matter leaves 'collecting' (or timeout) and prints the round summaries.
"""

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8765")
ANSWERS = {
    "alice": "我认为方案 A 更稳，主要顾虑是进度风险。",
    "bob": "我倾向方案 B，成本更低，但同意进度是关键风险。",
}


def digest(answers, notes):
    parts = []
    for item in sorted(answers, key=lambda a: a["question_id"]):
        parts.append(item["question_id"] + "\n" + item["content"] + "\n")
    parts.append(notes or "")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def client_for(token: str) -> Client:
    return Client(
        StreamableHttpTransport(
            url=f"{BASE}/mcp/", headers={"Authorization": f"Bearer {token}"}
        )
    )


async def agent_round(token: str, who: str) -> str | None:
    async with client_for(token) as c:
        listed = (await c.call_tool("list_pending_tasks", {})).data
        if not listed["tasks"]:
            print(f"[{who}] no pending tasks")
            return None
        task = listed["tasks"][0]
        detail = (await c.call_tool("get_task",
                                    {"task_id": task["task_id"]})).data
        print(f"[{who}] round {detail['round']['round_number']}, "
              f"previous_summary: {bool(detail['previous_summary'])}")
        answers = [
            {"question_id": q["question_id"],
             "content": f"{ANSWERS[who]}（{who} 第 "
                        f"{detail['round']['round_number']} 轮）"}
            for q in detail["round"]["questions"]
        ]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = (await c.call_tool(
            "submit_output",
            {
                "task_id": task["task_id"],
                "answers": answers,
                "notes": None,
                "human_approved": True,
                "approved_at": now,
                "content_digest": digest(answers, None),
                "idempotency_key": str(uuid.uuid4()),
            },
        )).data
        print(f"[{who}] submitted: {result['status']}")
        return task["matter_id"]


async def main() -> None:
    token_a = os.environ["SMOKE_TOKEN_ALICE"]
    token_b = os.environ["SMOKE_TOKEN_BOB"]
    matter_id = await agent_round(token_a, "alice")
    matter_id = await agent_round(token_b, "bob") or matter_id
    assert matter_id, "no task submitted; is the matter started?"
    # 两人都已提交 → 后台管线开始跑 LLM。轮询状态。
    async with client_for(token_a) as c:
        for attempt in range(60):
            status = (await c.call_tool(
                "get_matter_status", {"matter_id": matter_id})).data
            print(f"poll {attempt}: status={status['status']} "
                  f"rounds_total={status['rounds_total']}")
            if status["status"] != "collecting" or status["rounds_total"] >= 2:
                break
            await asyncio.sleep(5)
        for rnd in status["recent_rounds"]:
            for summary in rnd["summaries"]:
                print(f"--- round {rnd['round_number']} summary ---")
                print("convergence:", summary["convergence"])
                print("consensus:", summary["consensus_points"])
                print("divergences:", summary["divergences"])
                print("blind_spots:", summary["blind_spots"])
                print("open_questions:", summary["open_questions"])
        print("final status:", status["status"])


if __name__ == "__main__":
    asyncio.run(main())
