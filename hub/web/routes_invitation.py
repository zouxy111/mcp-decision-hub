"""API routes and HTML pages for invitation links."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from hub.api.passwords import hash_password
from hub.db.models import InvitationLink, Matter, User
from hub.domain import invitation_links
from hub.domain.agent_brief import build_agent_brief
from hub.web.deps import get_current_user, get_db, register_csrf_globals

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
    """创建邀请链接的请求。"""

    matter_id: str = Field(..., description="事项 ID")
    expires_in_days: int = Field(
        default=3, ge=1, le=365, description="有效期（天数）"
    )
    max_uses: int | None = Field(
        default=1, ge=1, description="最大使用次数（None 表示无限制）"
    )
    invited_name: str | None = Field(
        default=None, max_length=100, description="受邀人姓名（可选）"
    )


class InvitationLinkResponse(BaseModel):
    """邀请链接响应。"""

    id: str
    matter_id: str
    short_code: str
    full_url: str
    created_by: int
    expires_at: str
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
    expires_at: str | None = None
    agent_brief: str | None = None
    error: str | None = None


class ConsumeInvitationRequest(BaseModel):
    """使用邀请链接的请求。"""

    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
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
    """使用邀请链接响应。"""

    success: bool
    user_id: int | None = None
    username: str | None = None
    matter_id: str | None = None
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
    )
    db.commit()

    return InvitationLinkResponse(
        id=str(invitation.id),
        matter_id=invitation.matter_id,
        short_code=invitation.short_code,
        full_url=f"/invite/{invitation.short_code}",
        created_by=invitation.created_by,
        expires_at=invitation.expires_at.isoformat(),
        max_uses=invitation.max_uses,
        used_count=invitation.used_count,
        status=invitation.status,
        is_active=invitation.status == "active",
        created_at=invitation.created_at.isoformat(),
        invited_name=invitation.invited_name,
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
        expires_at=result.get("expires_at"),
        agent_brief=build_agent_brief(
            site_base=str(request.base_url).rstrip("/"),
            matter_title=result["matter_title"],
            invite_code=short_code,
            expires_at=result.get("expires_at"),
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
        
        # 消费邀请链接
        user, invitation = invitation_links.consume_invitation_link(
            db=db,
            short_code=short_code,
            username=request.username,
            email=request.email,
            password_hash=password_hash,
            ip_address=ip_address,
            user_agent=user_agent,
            display_name=request.display_name,
            responsibility=request.responsibility,
        )
        db.commit()

        return ConsumeInvitationResponse(
            success=True,
            user_id=user.id,
            username=user.username,
            matter_id=invitation.matter_id,
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
            expires_at=inv.expires_at.isoformat(),
            max_uses=inv.max_uses,
            used_count=inv.used_count,
            status=inv.status,
            is_active=inv.status == "active",
            created_at=inv.created_at.isoformat(),
            invited_name=inv.invited_name,
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
    """显示邀请接受页面（公开访问）。"""
    # 验证邀请链接
    validation = invitation_links.validate_invitation_link(db, short_code)
    
    invitation_info = None
    error = None
    
    if validation["valid"]:
        invitation_info = {
            "matter_title": validation["matter_title"],
            "creator_name": validation.get("creator_name", "系统管理员"),
            "invited_name": validation.get("invited_name"),
            "collaboration_mode": validation.get("collaboration_mode", "project"),
            "expires_at": validation.get("expires_at"),
        }
    else:
        error = validation.get("error", "邀请链接无效")
    
    return templates.TemplateResponse(
        request,
        "invitation_accept.html",
        {
            "short_code": short_code,
            "invitation_info": invitation_info,
            "error": error,
            "agent_brief": (
                build_agent_brief(
                    site_base=str(request.base_url).rstrip("/"),
                    matter_title=(invitation_info or {}).get("matter_title") or "",
                    invite_code=short_code,
                    expires_at=(invitation_info or {}).get("expires_at"),
                ) if invitation_info else None
            ),
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
