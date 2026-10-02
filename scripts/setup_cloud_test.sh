#!/bin/bash
# 云端多 Agent 协作测试 - 快速开始脚本
# 用途：自动在云端服务器创建测试账号并生成 Token

set -e

SERVER_IP="170.106.192.161"
SERVER_USER="ubuntu"
PROJECT_DIR="~/mcp-decision-hub"

echo "=========================================="
echo "云端多 Agent 协作测试 - 环境准备"
echo "=========================================="
echo ""

# 检查 SSH 连接
echo "1️⃣ 检查服务器连接..."
if ssh -o ConnectTimeout=5 ${SERVER_USER}@${SERVER_IP} "echo '连接成功'" > /dev/null 2>&1; then
    echo "✅ 服务器连接正常"
else
    echo "❌ 无法连接到服务器 ${SERVER_IP}"
    echo "请检查："
    echo "  - SSH 密钥是否配置"
    echo "  - 服务器 IP 是否正确"
    echo "  - 网络是否畅通"
    exit 1
fi

# 创建测试账号并生成 Token
echo ""
echo "2️⃣ 在云端创建测试账号和 Token..."
echo ""

ssh ${SERVER_USER}@${SERVER_IP} << 'ENDSSH'
cd ~/mcp-decision-hub

# 激活虚拟环境并运行 Python 脚本
.venv/bin/python << 'ENDPYTHON'
from sqlalchemy import select
from hub.api.tokens import issue_token
from hub.config import load_settings
from hub.db.models import User
from hub.db.session import init_db, make_engine, make_session_factory
from argon2 import PasswordHasher

settings = load_settings()
engine = make_engine(settings.database_url)
init_db(engine)
factory = make_session_factory(engine)

USERS = ["smoke_alice", "smoke_bob", "owner_test"]

print("创建测试账号...")
with factory() as session:
    hasher = PasswordHasher()
    created_count = 0
    for name in USERS:
        exists = session.scalar(select(User).where(User.username == name))
        if exists is None:
            session.add(
                User(
                    username=name,
                    email=f"{name}@example.com",
                    password_hash=hasher.hash("test-pw-123"),
                    is_admin=False,
                    is_active=True,
                    must_change_password=False
                )
            )
            created_count += 1
        else:
            print(f"  - {name}: 已存在")
    
    if created_count > 0:
        session.commit()
        print(f"✅ 创建了 {created_count} 个新账号")
    else:
        print("✅ 所有账号已存在")
    
    # 生成 Token
    print("\n生成 MCP Token...")
    alice_user = session.scalar(select(User).where(User.username == "smoke_alice"))
    bob_user = session.scalar(select(User).where(User.username == "smoke_bob"))
    
    _, token_a = issue_token(session, user=alice_user, name="test-token-alice")
    _, token_b = issue_token(session, user=bob_user, name="test-token-bob")
    
    session.commit()
    
    print("\n" + "="*60)
    print("测试环境准备完成")
    print("="*60)
    print("\n保存以下信息，稍后会用到：\n")
    print(f"ALICE_TOKEN={token_a}")
    print(f"BOB_TOKEN={token_b}")
    print("\nWeb 登录账号：")
    print("  Username: owner_test")
    print("  Password: test-pw-123")
    print("  URL: https://hub.tdp-demo.work/login")
ENDPYTHON
ENDSSH

echo ""
echo "3️⃣ 下一步操作："
echo ""
echo "  a) 复制上面的 ALICE_TOKEN 和 BOB_TOKEN"
echo ""
echo "  b) 访问 https://hub.tdp-demo.work/login"
echo "     使用 owner_test / test-pw-123 登录"
echo ""
echo "  c) 创建新事项："
echo "     - 标题：技术方案选型测试"
echo "     - 参与人：选择 smoke_alice 和 smoke_bob"
echo "     - 添加问题：你倾向哪个方案？"
echo "     - 点击「开始」"
echo ""
echo "  d) 运行测试脚本："
echo "     uv run python scripts/cloud_multi_agent_test.py \\"
echo "       --alice-token \"你的_ALICE_TOKEN\" \\"
echo "       --bob-token \"你的_BOB_TOKEN\""
echo ""
echo "=========================================="
