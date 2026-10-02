#!/usr/bin/env python3
"""
云端多 Agent 协作测试脚本
测试两个本地 Agent 通过云端服务器 (hub.tdp-demo.work) 进行决策协作

场景：
- Alice 和 Bob 两个用户，各自在本地运行 Agent
- 通过 MCP 协议连接到云端服务器
- Owner 在云端创建决策事项，邀请 Alice 和 Bob 参与
- Alice 和 Bob 各自提交立场
- 云端进行摘要、收敛判定、追问
- Owner 最终拍板

用法：
1. 在云端服务器创建测试账号（如果还没有）
2. 获取 Alice 和 Bob 的 MCP Token
3. 本地运行此脚本：
   python scripts/cloud_multi_agent_test.py \
     --alice-token <token> \
     --bob-token <token> \
     --matter-id <matter_id>
"""

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


class CloudAgentTester:
    """云端 Agent 协作测试器"""

    def __init__(self, base_url: str = "https://hub.tdp-demo.work"):
        self.base_url = base_url
        self.mcp_url = f"{base_url}/mcp/"

    def create_client(self, token: str) -> Client:
        """创建 MCP 客户端"""
        return Client(
            StreamableHttpTransport(
                url=self.mcp_url,
                headers={"Authorization": f"Bearer {token}"}
            )
        )

    def compute_content_hash(self, answers: list[dict], notes: str | None) -> str:
        """
        计算 content_hash（与服务器端一致的算法）
        
        参考 hub/domain/digest.py:compute_stance_content_hash
        """
        # 按 question_id 排序
        sorted_answers = sorted(answers, key=lambda a: a["question_id"])
        
        # 构建要哈希的内容
        parts = []
        for item in sorted_answers:
            parts.append(item["question_id"] + "\n" + item["content"] + "\n")
        if notes:
            parts.append(notes)
        
        # SHA256
        content = "".join(parts).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    async def get_user_info(self, token: str) -> dict[str, Any]:
        """获取当前用户信息"""
        async with self.create_client(token) as client:
            # 通过 list_pending_tasks 获取用户信息
            result = await client.call_tool("list_pending_tasks", {})
            return {
                "has_access": True,
                "pending_tasks": len(result.data.get("tasks", []))
            }

    async def list_tasks(self, token: str) -> list[dict]:
        """列出待办任务"""
        async with self.create_client(token) as client:
            result = await client.call_tool("list_pending_tasks", {})
            return result.data.get("tasks", [])

    async def get_task_detail(self, token: str, task_id: str) -> dict[str, Any]:
        """获取任务详情"""
        async with self.create_client(token) as client:
            result = await client.call_tool("get_task", {"task_id": task_id})
            return result.data

    async def submit_stance(
        self,
        token: str,
        task_id: str,
        answers: list[dict],
        notes: str | None = None,
        agent_name: str = "agent"
    ) -> dict[str, Any]:
        """提交立场"""
        content_hash = self.compute_content_hash(answers, notes)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        payload = {
            "task_id": task_id,
            "answers": answers,
            "notes": notes,
            "human_approved": True,
            "approved_at": now,
            "content_digest": content_hash,
            "idempotency_key": str(uuid.uuid4()),
        }
        
        print(f"[{agent_name}] 提交立场...")
        print(f"  - 回答数量: {len(answers)}")
        print(f"  - 备注: {notes or '无'}")
        print(f"  - content_hash: {content_hash[:16]}...")
        
        async with self.create_client(token) as client:
            result = await client.call_tool("submit_output", payload)
            return result.data

    async def get_matter_status(self, token: str, matter_id: str) -> dict[str, Any]:
        """获取事项状态"""
        async with self.create_client(token) as client:
            result = await client.call_tool("get_matter_status", {"matter_id": matter_id})
            return result.data

    async def wait_for_convergence(
        self,
        token: str,
        matter_id: str,
        timeout: int = 120,
        poll_interval: int = 5
    ) -> dict[str, Any]:
        """等待收敛或进入下一轮"""
        print(f"\n等待云端处理（最多 {timeout} 秒，每 {poll_interval} 秒检查一次）...")
        
        for attempt in range(timeout // poll_interval):
            await asyncio.sleep(poll_interval)
            
            status = await self.get_matter_status(token, matter_id)
            current_status = status["status"]
            rounds_total = status["rounds_total"]
            
            print(f"  [{attempt + 1}] 状态: {current_status}, 轮次: {rounds_total}")
            
            # 如果不在收集中，说明已经处理完成
            if current_status != "collecting":
                return status
            
            # 如果轮次增加了，说明进入下一轮
            if rounds_total > status.get("initial_rounds", 0):
                return status
        
        print("⚠️ 超时，返回最后状态")
        return await self.get_matter_status(token, matter_id)

    def print_round_summary(self, round_data: dict) -> None:
        """打印轮次摘要"""
        round_num = round_data["round_number"]
        print(f"\n{'='*60}")
        print(f"第 {round_num} 轮摘要")
        print(f"{'='*60}")
        
        for summary in round_data.get("summaries", []):
            print(f"\n收敛度: {summary.get('convergence', 'N/A')}")
            
            if summary.get("consensus_points"):
                print("\n✅ 共识点:")
                for point in summary["consensus_points"]:
                    print(f"  - {point}")
            
            if summary.get("divergences"):
                print("\n⚡ 分歧点:")
                for div in summary["divergences"]:
                    print(f"  - {div}")
            
            if summary.get("blind_spots"):
                print("\n👁️  盲区:")
                for spot in summary["blind_spots"]:
                    print(f"  - {spot}")
            
            if summary.get("open_questions"):
                print("\n❓ 追问:")
                for q in summary["open_questions"]:
                    print(f"  - {q}")


async def test_two_agents_collaboration(
    alice_token: str,
    bob_token: str,
    matter_id: str | None = None,
    alice_answer: str | None = None,
    bob_answer: str | None = None
):
    """测试两个 Agent 的协作流程"""
    
    tester = CloudAgentTester()
    
    print("\n" + "="*60)
    print("云端多 Agent 协作测试")
    print("="*60)
    
    # 1. 验证连接
    print("\n1️⃣ 验证 Agent 连接...")
    
    try:
        alice_info = await tester.get_user_info(alice_token)
        print(f"✅ Alice 已连接，待办任务: {alice_info['pending_tasks']}")
    except Exception as e:
        print(f"❌ Alice 连接失败: {e}")
        return
    
    try:
        bob_info = await tester.get_user_info(bob_token)
        print(f"✅ Bob 已连接，待办任务: {bob_info['pending_tasks']}")
    except Exception as e:
        print(f"❌ Bob 连接失败: {e}")
        return
    
    # 2. 获取任务列表
    print("\n2️⃣ 获取待办任务...")
    
    alice_tasks = await tester.list_tasks(alice_token)
    bob_tasks = await tester.list_tasks(bob_token)
    
    print(f"  Alice: {len(alice_tasks)} 个任务")
    print(f"  Bob: {len(bob_tasks)} 个任务")
    
    if not alice_tasks and not bob_tasks:
        print("\n⚠️ 没有待办任务")
        print("\n请先在云端创建决策事项并邀请 Alice 和 Bob 参与：")
        print("  1. 访问 https://hub.tdp-demo.work/")
        print("  2. 登录 Owner 账号")
        print("  3. 创建新事项")
        print("  4. 邀请 smoke_alice 和 smoke_bob")
        print("  5. 点击「开始」")
        return
    
    # 3. 获取任务详情
    print("\n3️⃣ 获取任务详情...")
    
    alice_task = alice_tasks[0] if alice_tasks else None
    bob_task = bob_tasks[0] if bob_tasks else None
    
    # 使用指定的 matter_id 或从任务中获取
    target_matter_id = matter_id or (alice_task["matter_id"] if alice_task else bob_task["matter_id"])
    
    # 找到对应的任务
    alice_task = next((t for t in alice_tasks if t["matter_id"] == target_matter_id), None)
    bob_task = next((t for t in bob_tasks if t["matter_id"] == target_matter_id), None)
    
    if not alice_task:
        print("⚠️ Alice 没有此事项的任务")
    if not bob_task:
        print("⚠️ Bob 没有此事项的任务")
    
    if not alice_task and not bob_task:
        print(f"❌ 两人都没有事项 {target_matter_id} 的任务")
        return
    
    # 获取详细信息
    if alice_task:
        alice_detail = await tester.get_task_detail(alice_token, alice_task["task_id"])
        print(f"\n📋 Alice 的任务:")
        print(f"  - 事项: {alice_detail['matter']['title']}")
        print(f"  - 轮次: {alice_detail['round']['round_number']}")
        print(f"  - 问题数: {len(alice_detail['round']['questions'])}")
        for q in alice_detail['round']['questions']:
            print(f"    • {q['text']}")
    
    if bob_task:
        bob_detail = await tester.get_task_detail(bob_token, bob_task["task_id"])
        print(f"\n📋 Bob 的任务:")
        print(f"  - 事项: {bob_detail['matter']['title']}")
        print(f"  - 轮次: {bob_detail['round']['round_number']}")
        print(f"  - 问题数: {len(bob_detail['round']['questions'])}")
        for q in bob_detail['round']['questions']:
            print(f"    • {q['text']}")
    
    # 4. 提交立场
    print("\n4️⃣ 提交立场...")
    
    # Alice 提交
    if alice_task:
        alice_answers = [
            {
                "question_id": q["question_id"],
                "content": alice_answer or f"我是 Alice，我认为应该选方案 A。理由：稳定性更高，风险可控。（轮次：{alice_detail['round']['round_number']}）"
            }
            for q in alice_detail['round']['questions']
        ]
        
        alice_result = await tester.submit_stance(
            alice_token,
            alice_task["task_id"],
            alice_answers,
            notes="Alice 的补充说明",
            agent_name="Alice"
        )
        print(f"  ✅ Alice 提交成功: {alice_result['status']}")
    
    # Bob 提交
    if bob_task:
        bob_answers = [
            {
                "question_id": q["question_id"],
                "content": bob_answer or f"我是 Bob，我倾向于方案 B。理由：成本更低，适合当前资源。（轮次：{bob_detail['round']['round_number']}）"
            }
            for q in bob_detail['round']['questions']
        ]
        
        bob_result = await tester.submit_stance(
            bob_token,
            bob_task["task_id"],
            bob_answers,
            notes="Bob 的额外考虑",
            agent_name="Bob"
        )
        print(f"  ✅ Bob 提交成功: {bob_result['status']}")
    
    # 5. 等待云端处理
    print("\n5️⃣ 等待云端处理...")
    
    token_for_status = alice_token if alice_task else bob_token
    status = await tester.wait_for_convergence(token_for_status, target_matter_id)
    
    # 6. 显示结果
    print("\n6️⃣ 处理结果...")
    print(f"\n最终状态: {status['status']}")
    print(f"总轮次: {status['rounds_total']}")
    
    for round_data in status.get("recent_rounds", []):
        tester.print_round_summary(round_data)
    
    print("\n" + "="*60)
    print("测试完成")
    print("="*60)
    
    # 返回摘要
    return {
        "matter_id": target_matter_id,
        "final_status": status["status"],
        "rounds_total": status["rounds_total"],
        "alice_submitted": alice_task is not None,
        "bob_submitted": bob_task is not None,
    }


async def main():
    parser = argparse.ArgumentParser(description="云端多 Agent 协作测试")
    parser.add_argument("--alice-token", required=True, help="Alice 的 MCP Token")
    parser.add_argument("--bob-token", required=True, help="Bob 的 MCP Token")
    parser.add_argument("--matter-id", help="指定的事项 ID（可选）")
    parser.add_argument("--alice-answer", help="Alice 的回答内容（可选）")
    parser.add_argument("--bob-answer", help="Bob 的回答内容（可选）")
    
    args = parser.parse_args()
    
    await test_two_agents_collaboration(
        alice_token=args.alice_token,
        bob_token=args.bob_token,
        matter_id=args.matter_id,
        alice_answer=args.alice_answer,
        bob_answer=args.bob_answer
    )


if __name__ == "__main__":
    asyncio.run(main())
