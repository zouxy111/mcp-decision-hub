"""Admin invitation page (PRD 3.1, M1 basic scope: create/resend/revoke)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import accounts
from hub.api.errors import ApiError
from hub.config import Settings
from hub.db.models import User
from hub.web.deps import get_db, get_settings, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")


def _pending_invitations(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.invitation_token_hash.is_not(None))
            .order_by(User.created_at.desc())
        ).all()
    )


def _render(request: Request, db: Session, admin: User, *, credential=None,
            credential_for=None, error=None, status_code=200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_invitations.html",
        {
            "pending": _pending_invitations(db),
            "credential": credential,
            "credential_for": credential_for,
            "error": error,
            "current_user_is_admin": True,
        },
        status_code=status_code,
    )


@router.get("/admin/invitations", response_class=HTMLResponse)
def invitations_page(request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    return _render(request, db, admin)


@router.post("/admin/invitations", response_class=HTMLResponse)
def invitations_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    settings: Settings = Depends(get_settings),
):
    if not username.strip() or not email.strip():
        return _render(request, db, admin, error="用户名与邮箱必填", status_code=422)
    user, token = accounts.create_invitation(
        db, admin=admin, username=username.strip(), email=email.strip(),
        ttl_seconds=settings.invite_ttl_seconds,
    )
    db.commit()
    return _render(request, db, admin, credential=token, credential_for=user.username)


@router.post("/admin/invitations/{user_id}/revoke")
def invitations_revoke(user_id: int, db: Session = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        accounts.revoke_invitation(db, admin=admin, user_id=user_id)
    except ApiError:
        pass
    db.commit()
    return RedirectResponse("/admin/invitations", status_code=303)
