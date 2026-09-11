"""JSON API 骨架：立场层。

与 hub/web/routes_*.py 的 HTML 路由不同，这里返回 JSON，认证走
Authorization: Bearer（无 Cookie 会话），错误经全局 ApiError handler 序列化。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from hub.api import stances as stance_svc
from hub.db.models import User
from hub.schemas.stance import StanceCreate, StanceRead
from hub.web.deps import get_db, require_bearer

router = APIRouter()


@router.post("/api/items/{matter_id}/stances", status_code=201,
             response_model=StanceRead)
def submit_stance(
    matter_id: str,
    payload: StanceCreate,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stance = stance_svc.create_stance(
        db, matter_id=matter_id, user=user, payload=payload
    )
    db.commit()
    return stance


@router.get("/api/items/{matter_id}/stances/{user_id}",
            response_model=StanceRead)
def read_stance(
    matter_id: str,
    user_id: int,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stance = stance_svc.get_stance(
        db, matter_id=matter_id, user=user, target_user_id=user_id
    )
    db.commit()  # 落审计（读也留痕）
    return stance


@router.get("/api/items/{matter_id}/stances",
            response_model=list[StanceRead])
def list_stances(
    matter_id: str,
    user: User = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    stances = stance_svc.list_stances(db, matter_id=matter_id, user=user)
    db.commit()
    return stances
