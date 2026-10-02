#!/usr/bin/env python3
"""
云端测试账号创建脚本（在服务器上运行）

部署到服务器后执行：
  cd ~/mcp-decision-hub
  .venv/bin/python scripts/create_test_accounts.py
"""

from sqlalchemy import select
from hub.api.tokens import issue_token
from hub.config import load_settings
from hub.db.models import User
from hub.db.session import init_db, make_engine, make_session_factory
from argon2 import PasswordHasher

USERS = [
    {"username": "smoke_alice", "email": "alice@example.com"},
    {"username": "smoke_bob", "email": "bob@example.com"},
    {"username": "owner_test", "email": "owner@example.com"},
]

PASSWORD = "test-pw-123"


def main():
    print("\n" + "="*60)
    print("云端测试环境准备")
    print("="*60)
    
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    hasher = PasswordHasher()
    
    print("\n1️⃣ 创建测试账号...")
    with factory() as session:
        created_count = 0
        for user_data in USERS:
            username = user_data["username"]
            exists = session.scalar(select(User).where(User.username == username))
            if exists is None:
                session.add(
                    User(
                        username=username,
                        email=user_data["email"],
                        password_hash=hasher.hash(PASSWORD),
                        is_admin=False,
                        is_active=True,
                        must_change_password=False
                    )
                )
                created_count += 1
                print(f"  ✅ 创建: {username}")
            else:
                print(f"  ⏭️  已存在: {username}")
        
        if created_count > 0:
            session.commit()
            print(f"\n成功创建 {created_count} 个新账号")
        
        # 生成 Token
        print("\n2️⃣ 生成 MCP Token...")
        alice_user = session.scalar(select(User).where(User.username == "smoke_alice"))
        bob_user = session.scalar(select(User).where(User.username == "smoke_bob"))
        
        _, token_a = issue_token(session, user=alice_user, name="test-token-alice")
        _, token_b = issue_token(session, user=bob_user, name="test-token-bob")
        
        session.commit()
        
        print("\n" + "="*60)
        print("✅ 测试环境准备完成")
        print("="*60)
        
        print("\n📋 保存以下信息到本地：\n")
        print(f"export ALICE_TOKEN='{token_a}'")
        print(f"export BOB_TOKEN='{token_b}'")
        
        print("\n" + "-"*60)
        print("🌐 Web 登录信息：")
        print("-"*60)
        print(f"  URL: https://hub.tdp-demo.work/login")
        print(f"  Username: owner_test")
        print(f"  Password: {PASSWORD}")
        
        print("\n" + "-"*60)
        print("📝 下一步操作：")
        print("-"*60)
        print("  1. 在本地保存上面的 Token")
        print("  2. 使用 owner_test 登录 Web 界面")
        print("  3. 创建新决策事项")
        print("  4. 邀请 smoke_alice 和 smoke_bob 参与")
        print("  5. 点击「开始」")
        print("  6. 在本地运行测试脚本：")
        print()
        print("     uv run python scripts/cloud_multi_agent_test.py \\")
        print("       --alice-token \"$ALICE_TOKEN\" \\")
        print("       --bob-token \"$BOB_TOKEN\"")
        print()


if __name__ == "__main__":
    main()
