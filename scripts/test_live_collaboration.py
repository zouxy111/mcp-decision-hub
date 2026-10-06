#!/usr/bin/env python3
"""实时测试多人协作系统 - 模拟张陈祎发起协作"""

import sys

from hub.api.passwords import hash_password
from hub.config import load_settings
from hub.db.models import Matter, User, new_id
from hub.db.session import init_db, make_engine, make_session_factory
from hub.domain import invitation_links


def main():
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    
    with session_factory() as db:
        print("=" * 80)
        print("🚀 MCP 决策中台 - 多人协作实时测试")
        print("=" * 80)
        print()
        
        # 1. 检查或创建张陈祎账号
        print("📝 步骤 1：准备张陈祎账号")
        print("-" * 80)
        
        from sqlalchemy import select
        existing_user = db.scalar(select(User).where(User.username == "zhangchenyi"))
        if existing_user:
            user = existing_user
            print(f"✅ 找到现有账号：{user.username} (ID: {user.id})")
        else:
            user = User(
                username="zhangchenyi",
                email="zhangchenyi@example.com",
                password_hash=hash_password("test123456"),
                is_active=True,
            )
            db.add(user)
            db.flush()
            print(f"✅ 创建新账号：{user.username} (ID: {user.id})")
        
        print()
        
        # 2. 创建一个产品协作项目
        print("📝 步骤 2：创建产品协作项目")
        print("-" * 80)
        
        matter = Matter(
            id=new_id("mat"),
            title="AI产品功能规划",
            goal="讨论AI助手的新功能设计和优先级",
            background="我们需要为下一版本规划新功能",
            initiator_id=user.id,
            status="open",
            timeout_seconds=settings.task_timeout_seconds,
            max_rounds=settings.max_rounds,
        )
        db.add(matter)
        db.flush()
        
        print("✅ 项目已创建")
        print(f"   标题: {matter.title}")
        print(f"   ID: {matter.id}")
        print(f"   状态: {matter.status}")
        print("   发起人: 张陈祎")
        print()
        
        # 3. 为团队成员生成邀请链接
        print("📝 步骤 3：生成邀请链接")
        print("-" * 80)
        
        team_members = [
            {"name": "李明", "uses": 10},
            {"name": "王芳", "uses": 10},
            {"name": "刘强", "uses": 10},
        ]
        
        invitations = []
        for member in team_members:
            inv = invitation_links.create_invitation_link(
                db=db,
                matter_id=matter.id,
                created_by=user.id,
                invited_name=member["name"],
                max_uses=member["uses"],
                expires_in_days=7,
            )
            invitations.append(inv)
            db.flush()
            
            print(f"✅ {member['name']} 的邀请链接：")
            print(f"   URL: http://localhost:8000/invite/{inv.short_code}")
            print(f"   短码: {inv.short_code}")
            print(f"   使用次数: 0/{inv.max_uses}")
            print(f"   有效期至: {inv.expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
            print()
        
        print()
        
        # 4. 创建一个会议协作
        print("📝 步骤 4：创建会议协作（无限制邀请）")
        print("-" * 80)
        
        meeting = Matter(
            id=new_id("mat"),
            title="Q4 产品规划会议",
            goal="讨论第四季度的产品路线图和资源分配",
            background="季度规划会议",
            initiator_id=user.id,
            status="open",
            timeout_seconds=settings.task_timeout_seconds,
            max_rounds=settings.max_rounds,
        )
        db.add(meeting)
        db.flush()
        
        print("✅ 会议已创建")
        print(f"   标题: {meeting.title}")
        print(f"   ID: {meeting.id}")
        print(f"   状态: {meeting.status}")
        print()
        
        # 生成会议邀请（无限制）
        meeting_inv = invitation_links.create_invitation_link(
            db=db,
            matter_id=meeting.id,
            created_by=user.id,
            invited_name=None,
            max_uses=None,
            expires_in_days=1,
        )
        db.flush()
        
        print("✅ 会议邀请链接（可供所有人使用）：")
        print(f"   URL: http://localhost:8000/invite/{meeting_inv.short_code}")
        print(f"   短码: {meeting_inv.short_code}")
        print("   使用次数: 无限制")
        print(f"   有效期至: {meeting_inv.expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        
        db.commit()
        
        # 5. 总结
        print()
        print("=" * 80)
        print("🎉 准备完成！现在可以在另一台电脑测试了")
        print("=" * 80)
        print()
        print("📋 测试说明：")
        print()
        print("方式1: 在另一台电脑的浏览器中访问（项目协作）")
        print("-" * 80)
        print(f"  李明的邀请: http://localhost:8000/invite/{invitations[0].short_code}")
        print(f"  王芳的邀请: http://localhost:8000/invite/{invitations[1].short_code}")
        print(f"  刘强的邀请: http://localhost:8000/invite/{invitations[2].short_code}")
        print()
        
        print("方式2: 在另一台电脑的浏览器中访问（会议协作）")
        print("-" * 80)
        print(f"  会议邀请: http://localhost:8000/invite/{meeting_inv.short_code}")
        print("  （这个链接可以被多人重复使用）")
        print()
        
        print("方式3: 使用 API 测试")
        print("-" * 80)
        print(f"  curl http://localhost:8000/api/invitations/{invitations[0].short_code}/validate")
        print()
        
        print("📌 注意：")
        print("  - 项目邀请每个限10次使用，7天有效")
        print("  - 会议邀请无限次使用，1天有效")
        print("  - 服务器运行在: http://localhost:8000")
        print("  - 如果另一台电脑在同一局域网，将 localhost 改为本机IP")
        print()
        
        # 获取本机 IP
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            print(f"💡 本机局域网IP: {local_ip}")
            print(f"   在另一台电脑用: http://{local_ip}:8000/invite/{invitations[0].short_code}")
            print()
        except Exception:
            pass
        
        # 保存信息到文件
        with open("invitation_links.txt", "w", encoding="utf-8") as f:
            f.write("=== 项目协作邀请链接 ===\n\n")
            for i, inv in enumerate(invitations):
                f.write(f"{team_members[i]['name']}: http://localhost:8000/invite/{inv.short_code}\n")
                f.write(f"短码: {inv.short_code}\n\n")
            
            f.write("\n=== 会议协作邀请链接 ===\n\n")
            f.write(f"会议: http://localhost:8000/invite/{meeting_inv.short_code}\n")
            f.write(f"短码: {meeting_inv.short_code}\n")
        
        print("✅ 邀请链接已保存到: invitation_links.txt")
        print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
