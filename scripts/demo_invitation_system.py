#!/usr/bin/env python3
"""
邀请链接系统演示脚本

演示项目协作和会议协作的完整流程。
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from contextlib import contextmanager

from hub.api.passwords import hash_password
from hub.config import load_settings
from hub.db.models import Matter, User
from hub.db.session import init_db, make_engine, make_session_factory
from hub.domain import invitation_links

# 全局引擎和会话工厂
_engine = None
_session_factory = None


def get_session_factory():
    """获取会话工厂。"""
    global _engine, _session_factory
    if _session_factory is None:
        config = load_settings()
        _engine = make_engine(config.database_url)
        init_db(_engine)
        _session_factory = make_session_factory(_engine)
    return _session_factory


@contextmanager
def get_db_session():
    """数据库会话上下文管理器。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def demo_project_collaboration():
    """演示项目协作流程。"""
    print("\n" + "=" * 60)
    print("演示 1：项目协作流程")
    print("=" * 60)
    
    with get_db_session() as db:
        # 1. 创建领导账号（或使用已存在的）
        print("\n步骤 1：创建领导账号")
        from sqlalchemy import select
        leader = db.execute(
            select(User).where(User.username == "leader")
        ).scalar_one_or_none()
        
        if leader:
            print(f"✓ 使用已存在的领导账号：{leader.username}")
        else:
            leader = User(
                username="leader",
                email="leader@example.com",
                password_hash=hash_password("password123"),
            )
            db.add(leader)
            db.commit()
            db.refresh(leader)
            print(f"✓ 领导账号创建成功：{leader.username}")
        
        # 2. 创建项目事项
        print("\n步骤 2：创建项目事项")
        matter = Matter(
            title="产品研发项目",
            goal="开发新一代产品",
            initiator_id=leader.id,
            mode="project",  # 项目模式
            status="draft",
        )
        db.add(matter)
        db.commit()
        db.refresh(matter)
        print(f"✓ 项目事项创建成功：{matter.title}")
        print(f"  - ID: {matter.id}")
        print(f"  - 模式: {matter.mode}")
        
        # 3. 创建邀请链接
        print("\n步骤 3：创建邀请链接")
        invitation = invitation_links.create_invitation_link(
            db=db,
            matter_id=matter.id,
            created_by=leader.id,
            expires_in_days=7,
            max_uses=10,
            invited_name="张三",
        )
        db.commit()
        db.refresh(invitation)
        print("✓ 邀请链接创建成功")
        print(f"  - 短码: {invitation.short_code}")
        print(f"  - 完整链接: https://your-domain.com/invite/{invitation.short_code}")
        print(f"  - 受邀人: {invitation.invited_name}")
        print(f"  - 有效期: {invitation.expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  - 最多使用: {invitation.max_uses} 次")
        
        # 4. 验证邀请链接
        print("\n步骤 4：验证邀请链接")
        validation = invitation_links.validate_invitation_link(db, invitation.short_code)
        print(f"✓ 邀请链接有效: {validation['valid']}")
        print(f"  - 项目名称: {validation['matter_title']}")
        print(f"  - 协作模式: {validation['collaboration_mode']}")
        
        # 5. 成员接受邀请（注册并加入）
        print("\n步骤 5：成员接受邀请")
        member_user, used_invitation = invitation_links.consume_invitation_link(
            db=db,
            short_code=invitation.short_code,
            username="zhangsan",
            email="zhangsan@example.com",
            password_hash=hash_password("password123"),
            ip_address="192.168.1.100",
            user_agent="Mozilla/5.0",
        )
        db.commit()
        print("✓ 成员注册并加入成功")
        print(f"  - 用户名: {member_user.username}")
        print(f"  - 邮箱: {member_user.email}")
        print(f"  - 已使用次数: {used_invitation.used_count}/{used_invitation.max_uses}")
        
        # 6. 查看事项的所有邀请链接
        print("\n步骤 6：查看项目的所有邀请链接")
        invitations = invitation_links.get_matter_invitations(db, matter.id)
        print(f"✓ 找到 {len(invitations)} 个邀请链接")
        for inv in invitations:
            print(f"  - {inv.short_code}: {inv.status}, 使用 {inv.used_count} 次")
        
        print("\n✅ 项目协作流程演示完成！")
        return invitation.short_code


def demo_meeting_collaboration():
    """演示会议协作流程。"""
    print("\n" + "=" * 60)
    print("演示 2：会议协作流程")
    print("=" * 60)
    
    with get_db_session() as db:
        # 1. 创建会议组织者账号（或使用已存在的）
        print("\n步骤 1：创建会议组织者账号")
        from sqlalchemy import select
        organizer = db.execute(
            select(User).where(User.username == "organizer")
        ).scalar_one_or_none()
        
        if organizer:
            print(f"✓ 使用已存在的组织者账号：{organizer.username}")
        else:
            organizer = User(
                username="organizer",
                email="organizer@example.com",
                password_hash=hash_password("password123"),
            )
            db.add(organizer)
            db.commit()
            db.refresh(organizer)
            print(f"✓ 组织者账号创建成功：{organizer.username}")
        
        # 2. 创建会议事项
        print("\n步骤 2：创建会议事项")
        meeting = Matter(
            title="Q1 产品规划会议",
            goal="确定 2024 Q1 产品路线图",
            initiator_id=organizer.id,
            mode="meeting",  # 会议模式
            status="draft",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        print(f"✓ 会议事项创建成功：{meeting.title}")
        print(f"  - ID: {meeting.id}")
        print(f"  - 模式: {meeting.mode}")
        
        # 3. 创建会议邀请链接（无限制使用）
        print("\n步骤 3：创建会议邀请链接")
        invitation = invitation_links.create_invitation_link(
            db=db,
            matter_id=meeting.id,
            created_by=organizer.id,
            expires_in_days=1,  # 会议链接通常有效期短
            max_uses=None,      # 无限制，方便多人快速加入
            invited_name=None,  # 不指定特定人员
        )
        db.commit()
        db.refresh(invitation)
        print("✓ 会议邀请链接创建成功")
        print(f"  - 短码: {invitation.short_code}")
        print(f"  - 完整链接: https://your-domain.com/invite/{invitation.short_code}")
        print(f"  - 有效期: {invitation.expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
        print("  - 使用次数: 无限制")
        
        # 4. 多个参与者快速加入
        print("\n步骤 4：多个参与者快速加入")
        participants = [
            ("wangwu", "wangwu@example.com"),
            ("zhaoliu", "zhaoliu@example.com"),
            ("sunqi", "sunqi@example.com"),
        ]
        
        for username, email in participants:
            user, _ = invitation_links.consume_invitation_link(
                db=db,
                short_code=invitation.short_code,
                username=username,
                email=email,
                password_hash=hash_password("password123"),
            )
            db.commit()
            print(f"  ✓ {username} 加入会议")
        
        # 5. 查看使用情况
        print("\n步骤 5：查看邀请链接使用情况")
        db.refresh(invitation)
        print("✓ 邀请链接统计")
        print(f"  - 已使用次数: {invitation.used_count}")
        print(f"  - 状态: {invitation.status}")
        print(f"  - 是否活跃: {invitation.status == 'active'}")
        
        # 6. 会议结束后撤销邀请链接
        print("\n步骤 6：会议结束，撤销邀请链接")
        invitation_links.revoke_invitation(db, str(invitation.id), organizer.id)
        db.commit()
        db.refresh(invitation)
        print("✓ 邀请链接已撤销")
        print(f"  - 状态: {invitation.status}")
        print(f"  - 撤销时间: {invitation.revoked_at.strftime('%Y-%m-%d %H:%M:%S')}")
        
        print("\n✅ 会议协作流程演示完成！")


def demo_security_features():
    """演示安全特性。"""
    print("\n" + "=" * 60)
    print("演示 3：安全特性")
    print("=" * 60)
    
    with get_db_session() as db:
        # 1. 创建测试用户和事项（或使用已存在的）
        from sqlalchemy import select
        user = db.execute(
            select(User).where(User.username == "security_test")
        ).scalar_one_or_none()
        
        if not user:
            user = User(
                username="security_test",
                email="security@example.com",
                password_hash=hash_password("password123"),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        
        matter = Matter(
            title="安全测试项目",
            goal="测试安全特性",
            initiator_id=user.id,
            status="draft",
        )
        db.add(matter)
        db.commit()
        db.refresh(matter)
        
        # 2. 创建限制使用次数的邀请
        print("\n测试 1：使用次数限制")
        invitation = invitation_links.create_invitation_link(
            db=db,
            matter_id=matter.id,
            created_by=user.id,
            expires_in_days=7,
            max_uses=1,  # 只能使用一次
        )
        db.commit()
        print(f"✓ 创建单次使用邀请：{invitation.short_code}")
        
        # 第一次使用
        user1, _ = invitation_links.consume_invitation_link(
            db=db,
            short_code=invitation.short_code,
            username="user1",
            email="user1@example.com",
            password_hash=hash_password("password123"),
        )
        db.commit()
        print(f"  ✓ 第一次使用成功：{user1.username}")
        
        # 尝试第二次使用
        try:
            invitation_links.consume_invitation_link(
                db=db,
                short_code=invitation.short_code,
                username="user2",
                email="user2@example.com",
                password_hash=hash_password("password123"),
            )
            print("  ✗ 应该失败但成功了")
        except invitation_links.InvitationExhaustedError:
            print("  ✓ 第二次使用被拒绝（已用尽）")
        
        # 3. 测试重复用户名检查
        print("\n测试 2：重复用户名检查")
        invitation2 = invitation_links.create_invitation_link(
            db=db,
            matter_id=matter.id,
            created_by=user.id,
            expires_in_days=7,
        )
        db.commit()
        
        try:
            invitation_links.consume_invitation_link(
                db=db,
                short_code=invitation2.short_code,
                username="user1",  # 重复的用户名
                email="different@example.com",
                password_hash=hash_password("password123"),
            )
            print("  ✗ 应该失败但成功了")
        except ValueError as e:
            print(f"  ✓ 重复用户名被拒绝：{str(e)}")
        
        # 4. 测试撤销功能
        print("\n测试 3：撤销邀请")
        invitation_links.revoke_invitation(db, str(invitation2.id), user.id)
        db.commit()
        print(f"  ✓ 邀请已撤销：{invitation2.short_code}")
        
        try:
            invitation_links.consume_invitation_link(
                db=db,
                short_code=invitation2.short_code,
                username="user3",
                email="user3@example.com",
                password_hash=hash_password("password123"),
            )
            print("  ✗ 应该失败但成功了")
        except invitation_links.InvitationRevokedError:
            print("  ✓ 使用已撤销的邀请被拒绝")
        
        print("\n✅ 安全特性演示完成！")


def main():
    """主函数。"""
    print("\n" + "=" * 60)
    print("MCP 决策中台 - 邀请链接系统演示")
    print("=" * 60)
    
    # 清理旧数据库
    import os
    db_path = "hub.db"  # 使用实际的数据库文件名
    if os.path.exists(db_path):
        print("\n清理旧数据库...")
        os.remove(db_path)
        print("✓ 旧数据库已清理")
    
    # 初始化数据库（通过第一次调用 get_session_factory 自动完成）
    print("\n初始化数据库...")
    get_session_factory()
    print("✓ 数据库初始化完成")
    
    try:
        # 演示 1：项目协作
        demo_project_collaboration()
        
        # 演示 2：会议协作
        demo_meeting_collaboration()
        
        # 演示 3：安全特性
        demo_security_features()
        
        print("\n" + "=" * 60)
        print("所有演示完成！")
        print("=" * 60)
        print("\n📚 更多信息：")
        print("  - 实现文档: docs/invitation-system-implementation.md")
        print("  - 使用指南: docs/invitation-system-user-guide.md")
        print("  - 完整报告: docs/multi-agent-collaboration-report.md")
        print()
        
    except Exception as e:
        print(f"\n❌ 演示过程中出错：{e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
