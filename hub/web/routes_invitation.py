"""API routes and HTML pages for invitation links."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from hub.api.passwords import hash_password
from hub.api.tokens import issue_token
from hub.db.models import InvitationLink, Matter, User
from hub.domain import invitation_links
from hub.domain.agent_brief import build_agent_brief
from hub.web.deps import (get_current_user, get_db, register_csrf_globals,
                          require_bearer, require_csrf)

# 2026-10-07：补``/api`` 前缀。此前 6 个 JSON 端点用的是裸路径
# （``/create``、``/validate/{code}``、``/{id}/revoke``），与站点其他路由
# 混在一起、且撞名风险高（``POST /create`` 挂在站点根上）。
# 加前缀后前端按 ``/api/invitations/*`` 寻址，与 ``routes_api`` /
# ``routes_auth_api`` 一致。旧路径不再暴露——本项目未带外部流量，
# 且裸路径本身就不该是公开契约。
router = APIRouter(prefix="/api/invitations", tags=["invitations"])
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


# ==================== Request/Response Models ====================


class CreateInvitationRequest(BaseModel):
    """创建邀请链接的请求。

    2026-10-12 owner 裁定：链接默认**永久**（expires_in_days 留空即不设时限，
    访问管理靠吊销）；按人绑定 invited_username（选填）后，落地页与 Agent
    都能走「有账号输密码 / 没账号注册」的分支。
    """

    matter_id: str = Field(..., description="事项 ID")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650,
        description="有效期（天数）；None = 永久",
    )
    max_uses: int | None = Field(
        default=1, ge=1, description="最大使用次数（None 表示无限制）"
    )
    invited_name: str | None = Field(
        default=None, max_length=100, description="受邀人姓名（可选）"
    )
    invited_username: str | None = Field(
        default=None, max_length=64,
        description="受邀用户名（可选，绑定到人）",
    )


class InvitationLinkResponse(BaseModel):
    """邀请链接响应。"""

    id: str
    matter_id: str
    short_code: str
    full_url: str
    created_by: int
    expires_at: str | None
    max_uses: int | None
    used_count: int
    status: str
    # 便捷字段：``status == "active"``。模型里只有 status（取值
    # active/consumed/expired/revoked），没有 is_active 列——此前
    # 响应模型声明了 is_active 又去读 invitation.is_active，创建与列举
    # 两个端点必然 AttributeError。整个 HTTP 邀请接口此前从没能跑通。
    is_active: bool
    created_at: str
    invited_name: str | None = None
    invited_username: str | None = None


class ValidateInvitationResponse(BaseModel):
    """验证邀请链接响应。

    2026-10-05：新增 ``agent_brief`` —— 一段 markdown，直接把「受邀人怎么注册、
    他的 AI 助手怎么接入、怎么上传文件、必须本人同意」交代给对方的 agent，
    对方 agent 只要 GET 这个接口就能拿到接入说明。
    """

    valid: bool
    matter_id: str | None = None
    matter_title: str | None = None
    invited_name: str | None = None
    invited_username: str | None = None
    # 绑定到人时的账号状态：True=已注册（只问密码即可入板）、
    # False=未注册（走注册）、None=未绑定
    account_exists: bool | None = None
    expires_at: str | None = None
    agent_brief: str | None = None
    error: str | None = None


class ConsumeInvitationRequest(BaseModel):
    """使用邀请链接的请求。

    2026-10-12：email 改为选填——Agent 代劳接入时往往不知道本人邮箱，
    缺省自动生成占位邮箱（仅用于满足唯一约束，不发信）。
    """

    username: str = Field(..., min_length=3, max_length=50)
    email: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    )
    password: str = Field(..., min_length=6)
    # 留言板形态要求先做个简单自我介绍：叫什么、负责什么（2026-10-04）。
    # 与 HTML 表单同一口径 —— 两个入口都必填，不做「页面严、接口松」的缺口。
    display_name: str = Field(
        ..., min_length=1, max_length=100, description="真实姓名（怎么称呼你）"
    )
    responsibility: str = Field(
        ..., min_length=1, max_length=2000, description="你在本次协作里负责什么"
    )


class ConsumeInvitationResponse(BaseModel):
    """使用邀请链接响应。

    2026-10-12：成功时直接返回 ``access_token``——Agent 代劳接入的
    场景下注册完即拿到令牌，省掉「再去登录换令牌」这一跳。
    明文只在此出现一次，之后只能在 /settings/agents 重签。
    """

    success: bool
    user_id: int | None = None
    username: str | None = None
    matter_id: str | None = None
    access_token: str | None = None
    error: str | None = None


class JoinInvitationRequest(BaseModel):
    """已有账号经邀请链接加入事项（Agent 代劳「下次接入」路径）。

    调用前提：已用账号密码换得 Bearer Token（POST /api/auth/token）。
    名片给了就更新，不给保持原样。
    """

    display_name: str | None = Field(default=None, max_length=100)
    responsibility: str | None = Field(default=None, max_length=2000)


class JoinInvitationResponse(BaseModel):
    success: bool
    matter_id: str | None = None
    username: str | None = None
    error: str | None = None


# ==================== API Endpoints ====================


@router.post("/create", response_model=InvitationLinkResponse)
def create_invitation(
    request: CreateInvitationRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    """创建邀请链接。

    只有事项的创建者或管理员可以创建邀请链接。
    """
    # 检查用户是否有权限
    matter = db.get(Matter, request.matter_id)
    if not matter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found"
        )

    if matter.initiator_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only matter owner can create invitations",
        )

    # 创建邀请链接
    invitation = invitation_links.create_invitation_link(
        db=db,
        matter_id=request.matter_id,
        created_by=user.id,
        expires_in_days=request.expires_in_days,
        max_uses=request.max_uses,
        invited_name=request.invited_name,
        invited_username=request.invited_username,
    )
    db.commit()

    return InvitationLinkResponse(
        id=str(invitation.id),
        matter_id=invitation.matter_id,
        short_code=invitation.short_code,
        full_url=f"/invite/{invitation.short_code}",
        created_by=invitation.created_by,
        expires_at=(invitation.expires_at.isoformat()
                    if invitation.expires_at else None),
        max_uses=invitation.max_uses,
        used_count=invitation.used_count,
        status=invitation.status,
        is_active=invitation.status == "active",
        created_at=invitation.created_at.isoformat(),
        invited_name=invitation.invited_name,
        invited_username=invitation.invited_username,
    )


@router.get("/validate/{short_code}", response_model=ValidateInvitationResponse)
def validate_invitation_route(
    short_code: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """验证邀请链接是否有效（公开）。

    返回里带 ``agent_brief``：对方 agent 读这一段就知道怎么注册、怎么接入、
    怎么上传文件、以及「上传前必须本人同意」这条硬规矩。
    """
    result = invitation_links.validate_invitation_link(db, short_code)
    
    if not result["valid"]:
        return ValidateInvitationResponse(
            valid=False, 
            error=result.get("error", "Invalid invitation")
        )

    return ValidateInvitationResponse(
        valid=True,
        matter_id=result["matter_id"],
        matter_title=result["matter_title"],
        invited_name=result.get("invited_name"),
        invited_username=result.get("invited_username"),
        account_exists=result.get("account_exists"),
        expires_at=result.get("expires_at"),
        agent_brief=build_agent_brief(
            site_base=str(request.base_url).rstrip("/"),
            matter_title=result["matter_title"],
            invite_code=short_code,
            expires_at=result.get("expires_at"),
            invited_username=result.get("invited_username"),
            account_exists=result.get("account_exists"),
        ),
    )


@router.post("/consume/{short_code}", response_model=ConsumeInvitationResponse)
def consume_invitation_route(
    short_code: str,
    request: ConsumeInvitationRequest,
    req: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """使用邀请链接注册并加入事项。

    无需登录即可访问，用于新用户通过邀请链接注册。
    """
    try:
        # 获取 IP 和 User-Agent
        ip_address = req.client.host if req.client else None
        user_agent = req.headers.get("user-agent")

        # 哈希密码
        password_hash = hash_password(request.password)

        # Agent 代劳接入通常没有邮箱：自动生成占位邮箱（唯一约束用，不发信）
        email = request.email or f"{request.username}@onboard.local"

        # 消费邀请链接
        user, invitation = invitation_links.consume_invitation_link(
            db=db,
            short_code=short_code,
            username=request.username,
            email=email,
            password_hash=password_hash,
            ip_address=ip_address,
            user_agent=user_agent,
            display_name=request.display_name,
            responsibility=request.responsibility,
        )
        # 注册成功即签发 Agent Token（Agent 代劳路径免「再登录一次换令牌」）
        _, plaintext = issue_token(db, user=user, name="邀请接入")
        db.commit()

        return ConsumeInvitationResponse(
            success=True,
            user_id=user.id,
            username=user.username,
            matter_id=invitation.matter_id,
            access_token=plaintext,
        )

    except invitation_links.InvitationNotFoundError:
        return ConsumeInvitationResponse(
            success=False, error="Invitation not found"
        )
    except invitation_links.InvitationExpiredError:
        return ConsumeInvitationResponse(
            success=False, error="Invitation has expired"
        )
    except invitation_links.InvitationExhaustedError:
        return ConsumeInvitationResponse(
            success=False, error="Invitation has been fully used"
        )
    except invitation_links.InvitationRevokedError:
        return ConsumeInvitationResponse(
            success=False, error="Invitation has been revoked"
        )
    except ValueError as e:
        return ConsumeInvitationResponse(
            success=False, error=str(e)
        )


@router.post("/join/{short_code}", response_model=JoinInvitationResponse)
def join_invitation_route(
    short_code: str,
    request: JoinInvitationRequest,
    req: Request,
    user: Annotated[User, Depends(require_bearer)],
    db: Annotated[Session, Depends(get_db)],
):
    """已有账号经邀请链接加入事项（Bearer 认证）。

    Agent 代劳「下次接入」：拿本人账号密码调 /api/auth/token 换 Bearer，
    再调本端点消费新邀请码入板。不建号、不涉及密码。
    """
    try:
        invitation = invitation_links.join_matter_via_link(
            db=db,
            short_code=short_code,
            user=user,
            display_name=request.display_name,
            responsibility=request.responsibility,
            ip_address=req.client.host if req.client else None,
            user_agent=req.headers.get("user-agent"),
        )
        db.commit()
        return JoinInvitationResponse(
            success=True,
            matter_id=invitation.matter_id,
            username=user.username,
        )
    except invitation_links.InvitationNotFoundError:
        return JoinInvitationResponse(success=False, error="Invitation not found")
    except invitation_links.InvitationExpiredError:
        return JoinInvitationResponse(success=False, error="Invitation has expired")
    except invitation_links.InvitationExhaustedError:
        return JoinInvitationResponse(success=False, error="Invitation has been fully used")
    except invitation_links.InvitationRevokedError:
        return JoinInvitationResponse(success=False, error="Invitation has been revoked")


@router.get("/matter/{matter_id}", response_model=list[InvitationLinkResponse])
def get_matter_invitations_route(
    # Matter 主键是 ``mat_`` 前缀的字符串，声明成 int 会让 FastAPI 一律
    # 返 422「unable to parse string as integer」——这个端点因此从未可用过。
    matter_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    # 路由侧叫 include_inactive，服务侧的参数名是 include_revoked（排除
    # status=revoked）。此前两边名字不一致 → TypeError，此端点从未跑通。
    include_inactive: bool = False,
):
    """获取事项的所有邀请链接。

    只有事项的创建者或管理员可以查看。
    """
    # 检查权限
    matter = db.get(Matter, matter_id)
    if not matter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found"
        )

    if matter.initiator_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only matter owner can view invitations",
        )

    invitations = invitation_links.get_matter_invitations(
        db, matter_id, include_revoked=include_inactive
    )

    return [
        InvitationLinkResponse(
            id=str(inv.id),
            matter_id=inv.matter_id,
            short_code=inv.short_code,
            full_url=f"/invite/{inv.short_code}",
            created_by=inv.created_by,
            expires_at=(inv.expires_at.isoformat()
                        if inv.expires_at else None),
            max_uses=inv.max_uses,
            used_count=inv.used_count,
            status=inv.status,
            is_active=inv.status == "active",
            created_at=inv.created_at.isoformat(),
            invited_name=inv.invited_name,
            invited_username=inv.invited_username,
        )
        for inv in invitations
    ]


@router.post("/{invitation_id}/revoke")
def revoke_invitation_route(
    invitation_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    """撤销邀请链接。

    只有创建者或事项管理员可以撤销。
    """
    invitation = db.get(InvitationLink, invitation_id)
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )

    matter = db.get(Matter, invitation.matter_id)
    if not matter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Matter not found",
        )

    if matter.initiator_id != user.id and invitation.created_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creator or matter owner can revoke invitation",
        )

    invitation_links.revoke_invitation(db, invitation_id, user.id)
    db.commit()

    # 回一份刷新后的链接状态：前端拿到就能直接更新列表，不用再发一次查询。
    db.refresh(invitation)
    return {
        "success": True,
        "message": "Invitation revoked",
        "id": str(invitation.id),
        "matter_id": invitation.matter_id,
        "short_code": invitation.short_code,
        "status": invitation.status,
        "is_active": invitation.status == "active",
        "used_count": invitation.used_count,
    }


# ==================== HTML Pages ====================

# 邀请落地页是**发给外部人的公开链接**，必须挂在站点根（``/invite/{code}``）——
# 收件人看邮件里的地址就落地，不带任何前缀。它与 ``/api/invitations/*``
# 分属两个 router：JSON 给自家前端用，HTML 给被邀请人用。
pages_router = APIRouter(tags=["invitation-pages"])


@pages_router.get("/invite/{short_code}", response_class=HTMLResponse)
def show_invitation_page(
    short_code: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """显示邀请接受页面（公开访问）。

    2026-10-12 零页面接入：Agent 直接打开链接（``?for_agent=1`` 或
    Accept 偏好 text/markdown）时，返回接入说明纯文本而非网页——
    受邀人把链接转发给自己的 Agent 即可，全程不需要打开页面。
    """
    validation = invitation_links.validate_invitation_link(db, short_code)

    if validation["valid"]:
        brief = build_agent_brief(
            site_base=str(request.base_url).rstrip("/"),
            matter_title=validation.get("matter_title") or "",
            invite_code=short_code,
            expires_at=validation.get("expires_at"),
            invited_username=validation.get("invited_username"),
            account_exists=validation.get("account_exists"),
        )
        wants_text = (
            request.query_params.get("for_agent") == "1"
            or "text/markdown" in request.headers.get("accept", "")
            or "text/plain" in request.headers.get("accept", "")
        )
        if wants_text:
            return PlainTextResponse(brief, media_type="text/markdown; charset=utf-8")
        invitation_info = {
            "matter_title": validation["matter_title"],
            "creator_name": validation.get("creator_name", "系统管理员"),
            "invited_name": validation.get("invited_name"),
            "invited_username": validation.get("invited_username"),
            "account_exists": validation.get("account_exists"),
            "collaboration_mode": validation.get("collaboration_mode", "project"),
            "expires_at": validation.get("expires_at"),
        }
        error = None
    else:
        brief = None
        invitation_info = None
        error = validation.get("error", "邀请链接无效")
    
    return templates.TemplateResponse(
        request,
        "invitation_accept.html",
        {
            "short_code": short_code,
            "invitation_info": invitation_info,
            "error": error,
            "site_base": str(request.base_url).rstrip("/"),
            "agent_brief": brief,
        },
    )


def _invitation_info(db: Session, short_code: str) -> dict | None:
    """邀请页要展示的事项信息；链接无效时返回 None。"""
    validation = invitation_links.validate_invitation_link(db, short_code)
    if not validation["valid"]:
        return None
    return {
        "matter_title": validation["matter_title"],
        "creator_name": validation.get("creator_name", "系统管理员"),
        "invited_name": validation.get("invited_name"),
        "collaboration_mode": validation.get("collaboration_mode", "project"),
        "expires_at": validation.get("expires_at"),
    }


def _invite_error(request: Request, db: Session, short_code: str, message: str):
    """带错误信息重新渲染邀请页（保持用户已填的内容不必重打）。"""
    return templates.TemplateResponse(
        request,
        "invitation_accept.html",
        {
            "short_code": short_code,
            "invitation_info": _invitation_info(db, short_code),
            "error": message,
        },
        status_code=400,
    )


@pages_router.post("/invite/{short_code}/accept")
async def accept_invitation(
    short_code: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    expectations: Annotated[str | None, Form()] = None,
    background: Annotated[str | None, Form()] = None,
    display_name: Annotated[str | None, Form()] = None,
    responsibility: Annotated[str | None, Form()] = None,
):
    """处理邀请接受表单提交（公开访问）。"""
    # 先自我介绍、再建账号：留言板形态下「你是谁、你负责什么」是协作前提
    # （别人得知道在跟谁说话），所以这两项必填。2026-10-04 起。
    if not (display_name or "").strip() or not (responsibility or "").strip():
        return _invite_error(
            request, db, short_code, "请填写你的姓名，以及你在本次协作中负责的内容"
        )

    # 验证密码匹配
    if password != password_confirm:
        return _invite_error(request, db, short_code, "两次输入的密码不一致")
    
    try:
        # 获取 IP 和 User-Agent
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        
        # 哈希密码
        password_hash = hash_password(password)
        
        # 消费邀请链接（姓名 / 负责内容随注册一并写入参与人名片）
        user, invitation = invitation_links.consume_invitation_link(
            db=db,
            short_code=short_code,
            username=username,
            email=email,
            password_hash=password_hash,
            ip_address=ip_address,
            user_agent=user_agent,
            display_name=display_name,
            responsibility=responsibility,
        )
        db.commit()
        
        # 重定向到登录页，提示用户登录
        return RedirectResponse(
            url=f"/login?registered=1&username={username}",
            status_code=303,
        )
    
    except invitation_links.InvitationNotFoundError:
        error = "邀请链接不存在"
    except invitation_links.InvitationExpiredError:
        error = "邀请链接已过期"
    except invitation_links.InvitationExhaustedError:
        error = "邀请链接已达到使用上限"
    except invitation_links.InvitationRevokedError:
        error = "邀请链接已被撤销"
    except ValueError as e:
        error = str(e)
    
    # 如果出错，重新显示表单
    return _invite_error(request, db, short_code, error)


@pages_router.post("/invite/{short_code}/login_join")
async def login_join_invitation(
    short_code: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    """链接绑定的人已有账号：输密码 → 登录 + 入板，一步进板（2026-10-12）。

    公开端点（同 /accept）：能力凭证是邀请码本身。登录失败计数走与
    /login 相同的 username+IP 滑窗限流。
    """
    from hub.api import accounts
    from hub.web.deps import get_limiter, set_session_cookie

    settings = request.app.state.settings
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is not None:
        from hub.domain.rate_limit import (rate_limit_key_login_ip,
                                           rate_limit_key_login_username)
        for key, limit in (
            (rate_limit_key_login_username(username),
             settings.rate_limit_login_username_per_minute),
            (rate_limit_key_login_ip(request.client.host if request.client else ""),
             settings.rate_limit_login_ip_per_minute),
        ):
            ok, retry = limiter.check(key, limit=limit)
            if not ok:
                return _invite_error(
                    request, db, short_code,
                    f"尝试过于频繁，请 {retry} 秒后重试")

    user = accounts.authenticate(db, username=username, password=password)
    if user is None:
        from hub.domain.rate_limit import (rate_limit_key_login_ip,
                                           rate_limit_key_login_username)
        if limiter is not None:
            limiter.allow(rate_limit_key_login_username(username),
                          limit=settings.rate_limit_login_username_per_minute)
            limiter.allow(rate_limit_key_login_ip(
                request.client.host if request.client else ""),
                limit=settings.rate_limit_login_ip_per_minute)
        return _invite_error(request, db, short_code, "密码不对，请重试")

    try:
        invitation = invitation_links.join_matter_via_link(
            db=db,
            short_code=short_code,
            user=user,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
    except invitation_links.InvitationNotFoundError:
        return _invite_error(request, db, short_code, "邀请链接不存在")
    except invitation_links.InvitationExpiredError:
        return _invite_error(request, db, short_code, "邀请链接已过期")
    except invitation_links.InvitationExhaustedError:
        return _invite_error(request, db, short_code, "邀请链接已达到使用上限")
    except invitation_links.InvitationRevokedError:
        return _invite_error(request, db, short_code, "邀请链接已被撤销")

    response = RedirectResponse(f"/matters/{invitation.matter_id}",
                                status_code=303)
    set_session_cookie(response, request, user.id)
    return response


# ==================== 发起人邀请管理页（2026-10-12） ====================
# 链接 = （项目 × 人） 的访问阀门：一人一条专属链接、默认永不过期，
# 发起人在这里创建、查询、吊销；「移除参与人」在事项详情页参与人面板。


def _invitations_page_context(db: Session, matter: Matter) -> dict:
    invitations = invitation_links.get_matter_invitations(
        db, matter.id, include_revoked=True)
    return {
        "matter": matter,
        "invitations": invitations,
        "error": None,
    }


@pages_router.get("/matters/{matter_id}/invitations", response_class=HTMLResponse)
def matter_invitations_page(
    matter_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """发起人查看/管理本事项的全部邀请链接。"""
    matter = db.get(Matter, matter_id)
    if not matter:
        raise HTTPException(status_code=404, detail="事项不存在")
    if matter.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="仅发起人可管理邀请链接")
    return templates.TemplateResponse(
        request, "matter_invitations.html", _invitations_page_context(db, matter))


@pages_router.post("/matters/{matter_id}/invitations")
def matter_invitations_create(
    matter_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    invited_name: Annotated[str, Form()] = "",
    invited_username: Annotated[str, Form()] = "",
    expires_in_days: Annotated[str, Form()] = "",
    max_uses: Annotated[str, Form()] = "1",
):
    """创建邀请链接：有效天数留空 = 永久（owner 2026-10-12 裁定）。"""
    matter = db.get(Matter, matter_id)
    if not matter:
        raise HTTPException(status_code=404, detail="事项不存在")
    if matter.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="仅发起人可管理邀请链接")
    days = int(expires_in_days) if expires_in_days.strip() else None
    uses = int(max_uses) if max_uses.strip() else None
    invitation_links.create_invitation_link(
        db=db,
        matter_id=matter_id,
        created_by=user.id,
        invited_name=invited_name.strip() or None,
        invited_username=invited_username.strip() or None,
        expires_in_days=days,
        max_uses=uses,
    )
    db.commit()
    return RedirectResponse(f"/matters/{matter_id}/invitations",
                            status_code=303)


@pages_router.post("/matters/{matter_id}/invitations/{invitation_id}/revoke")
def matter_invitations_revoke(
    matter_id: str,
    invitation_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_csrf)],
):
    """吊销链接：挡新加入；已在板上的去事项详情页「移除参与人」。"""
    matter = db.get(Matter, matter_id)
    if not matter:
        raise HTTPException(status_code=404, detail="事项不存在")
    if matter.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="仅发起人可管理邀请链接")
    result = invitation_links.revoke_invitation_link(db, invitation_id)
    db.commit()
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return RedirectResponse(f"/matters/{matter_id}/invitations",
                            status_code=303)
