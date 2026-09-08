"""Agent token management page (FR-02)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from hub.api import tokens as token_svc
from hub.api.errors import ApiError
from hub.db.models import User
from hub.web.deps import get_current_user, get_db, register_csrf_globals, require_csrf

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


def _render(request: Request, db: Session, user: User, *, new_plaintext=None,
            new_name=None, error=None, status_code=200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "agents.html",
        {
            "tokens": token_svc.list_tokens(db, user=user),
            "new_plaintext": new_plaintext,
            "new_name": new_name,
            "error": error,
            "current_user_is_admin": user.is_admin,
        },
        status_code=status_code,
    )


@router.get("/settings/agents", response_class=HTMLResponse)
def agents_page(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    return _render(request, db, user)


@router.post("/settings/agents", response_class=HTMLResponse)
def agents_create(request: Request, name: str = Form(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(require_csrf)):
    if not name.strip():
        return _render(request, db, user, error="名称不能为空", status_code=422)
    _, plaintext = token_svc.issue_token(db, user=user, name=name.strip())
    db.commit()
    return _render(request, db, user, new_plaintext=plaintext, new_name=name.strip())


@router.post("/settings/agents/{token_id}/revoke")
def agents_revoke(token_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_csrf)):
    try:
        token_svc.revoke_token(db, user=user, token_id=token_id)
    except ApiError:
        pass  # already gone or not yours; the list re-render shows the truth
    db.commit()
    return RedirectResponse("/settings/agents", status_code=303)
