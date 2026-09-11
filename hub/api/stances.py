"""立场服务层。

与 hub/api/*.py 其余模块一致：零 FastAPI 依赖，签名 (session, *, ...)，
事务由调用方（路由）负责提交。

权限裁定（本模块是唯一事实源）：
- 「事项成员」= 发起人或参与人，复用 matters.get_matter_for_user；
- 可见性 = 成员 且（visibility=all，或该成员本人是参与人）；
- 提交立场 = 必须是该事项参与人（发起人但未参与也不行）；
- 一切不满足的情形统一 404 RESOURCE_NOT_FOUND，且文案与「事项不存在」
  一致，避免通过状态码/文案泄露「该事项存在」。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import audit
from hub.api import matters as matter_svc
from hub.api.errors import ApiError
from hub.db.models import Matter, Stance, User
from hub.schemas.stance import StanceCreate, Visibility

NOT_FOUND_MESSAGE = "事项不存在"
STANCE_NOT_FOUND_MESSAGE = "立场不存在"


def _require_matter_access(
    session: Session, *, matter_id: str, user: User
) -> Matter:
    """读侧入口：事项成员（发起人或参与人）放行，其余一律 404。"""
    matter = matter_svc.get_matter_for_user(
        session, matter_id=matter_id, user=user
    )
    if matter is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)
    return matter


def _is_participant(session: Session, *, matter_id: str, user_id: int) -> bool:
    return matter_svc.is_participant(session, matter_id=matter_id,
                                     user_id=user_id)


def _visible(
    session: Session, *, matter: Matter, user: User, stance: Stance
) -> bool:
    """成员基础上的可见性过滤。调用方须已通过 _require_matter_access。"""
    if stance.visibility == Visibility.PARTICIPANTS.value:
        return _is_participant(session, matter_id=matter.id, user_id=user.id)
    return True


def create_stance(
    session: Session,
    *,
    matter_id: str,
    user: User,
    payload: StanceCreate,
) -> Stance:
    """把已通过 Schema 校验的载荷落为一条立场记录（同一轮同人唯一）。

    先校验事项存在且当前用户是参与人：否则 SQLite 外键会抛 IntegrityError
    变成 500，这里必须提前拦成 404。
    """
    if session.get(Matter, matter_id) is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)
    if not _is_participant(session, matter_id=matter_id, user_id=user.id):
        raise ApiError(404, "RESOURCE_NOT_FOUND", NOT_FOUND_MESSAGE)

    stance = Stance(
        matter_id=matter_id,
        user_id=user.id,
        round_number=payload.round_number,
        stance=payload.stance.value,
        confidence=payload.confidence,
        position_summary=payload.position_summary,
        rationale_summary=payload.rationale_summary,
        non_negotiables=list(payload.non_negotiables),
        conditions=list(payload.conditions),
        open_questions=list(payload.open_questions),
        depends_on=list(payload.depends_on),
        questions_for=[q.model_dump() for q in payload.questions_for],
        disagreement_kind=(
            payload.disagreement_kind.value if payload.disagreement_kind else None
        ),
        supersedes=payload.supersedes,
        acting_as=payload.acting_as.value,
        authority=payload.authority,
        ttl_seconds=payload.ttl_seconds,
        urgency=payload.urgency.value,
        visibility=payload.visibility.value,
        content_hash=payload.content_hash,
    )
    session.add(stance)
    session.flush()
    return stance


def get_stance(
    session: Session,
    *,
    matter_id: str,
    user: User,
    target_user_id: int,
) -> Stance:
    """读取某位参与人在该事项上最新一轮的立场，并写审计。

    不存在的 user_id、跨事项的 user_id、以及可见性不足，一律 404。
    """
    matter = _require_matter_access(session, matter_id=matter_id, user=user)
    stance = session.scalar(
        select(Stance)
        .where(Stance.matter_id == matter_id, Stance.user_id == target_user_id)
        .order_by(Stance.round_number.desc())
    )
    if stance is None or not _visible(session, matter=matter, user=user,
                                      stance=stance):
        raise ApiError(404, "RESOURCE_NOT_FOUND", STANCE_NOT_FOUND_MESSAGE)
    audit.record_audit(
        session, audit.STANCE_READ,
        actor_user_id=user.id, matter_id=matter_id,
        detail={"target_user_id": target_user_id, "stance_id": stance.stance_id},
    )
    return stance


def list_stances(session: Session, *, matter_id: str, user: User) -> list[Stance]:
    """列出该事项下当前用户可见的全部立场（按轮次升序）。"""
    matter = _require_matter_access(session, matter_id=matter_id, user=user)
    rows = session.scalars(
        select(Stance)
        .where(Stance.matter_id == matter_id)
        .order_by(Stance.round_number.asc(), Stance.created_at.asc())
    ).all()
    return [s for s in rows if _visible(session, matter=matter, user=user,
                                        stance=s)]
