"""Admin invitation page (PRD 3.1, M1 basic scope: create/resend/revoke),
read-only audit query page (FR-23b), ops observability page (PRD 10.4), and
LLM model configuration page (added 2026-09-19)."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.api import accounts
from hub.api import audit as audit_svc
from hub.api.audit_query import query_audit_events
from hub.api.errors import ApiError
from hub.config import Settings
from hub.db.models import AuditEvent, User
from hub.llm.runtime import (
    ALLOWED_MODELS,
    API_KEY_CONSOLE_URL,
    DEFAULT_BASE_URL,
    RETIRED_MODELS,
    ConfigValidationError,
    LLMError,
    mask_secret,
    read_config,
    resolve_effective,
    save_config,
)
from hub.web.deps import (
    get_db,
    get_settings,
    register_csrf_globals,
    require_admin,
    require_csrf_admin,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


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
    admin: User = Depends(require_csrf_admin),
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
                       admin: User = Depends(require_csrf_admin)):
    try:
        accounts.revoke_invitation(db, admin=admin, user_id=user_id)
    except ApiError:
        pass
    db.commit()
    return RedirectResponse("/admin/invitations", status_code=303)


def _parse_date(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(422, "VALIDATION_FAILED",
                       "日期格式应为 YYYY-MM-DD") from None
    return parsed + timedelta(days=1) if end_of_day else parsed


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_page(
    request: Request,
    actor: str = "",
    matter_id: str = "",
    event_type: str = "",
    since: str = "",
    until: str = "",
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    error = None
    try:
        since_dt = _parse_date(since or None)
        until_dt = _parse_date(until or None, end_of_day=True)
    except ApiError as e:
        error = e.message
        since_dt = until_dt = None
    actor_user_id = None
    if actor.strip():
        found = db.scalar(select(User.id).where(User.username == actor.strip()))
        actor_user_id = found if found is not None else -1  # 未知账号 → 空结果
    rows, has_more = ([], False)
    if error is None:
        rows, has_more = query_audit_events(
            db, actor_user_id=actor_user_id, matter_id=matter_id.strip() or None,
            event_type=event_type.strip() or None,
            since=since_dt, until=until_dt, offset=max(0, offset),
        )
    usernames = {
        u.id: u.username
        for u in db.scalars(
            select(User).where(
                User.id.in_([r.actor_user_id for r in rows
                             if r.actor_user_id is not None] or [0])
            )
        ).all()
    }
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "rows": rows,
            "usernames": usernames,
            "has_more": has_more,
            "offset": max(0, offset),
        "filters": {"actor": actor, "matter_id": matter_id,
                    "event_type": event_type, "since": since,
                    "until": until},
        "error": error,
        "current_user_is_admin": True,
    },
)


@router.get("/admin/ops", response_class=HTMLResponse)
def admin_ops_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """调度器运行状态与队列深度（PRD 10.4 可观测）。仅管理员。"""
    from hub.api import audit as audit_svc
    from hub.api.scheduler import SCHEDULER_STATE

    state = SCHEDULER_STATE
    # 队列深度：从 app.state 获取（main.py lifespan 设置）
    app = request.app
    drive_qsize = (app.state.drive_queue.qsize()
                   if hasattr(app.state, "drive_queue") else 0)
    resume_qsize = (app.state.resume_queue.qsize()
                    if hasattr(app.state, "resume_queue") else 0)
    # 最近 task_timeout / task_reassigned 审计（各最多 25 条）
    timeout_events = db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit_svc.TASK_TIMEOUT)
        .order_by(AuditEvent.created_at.desc()).limit(25)
    ).all()
    reassign_events = db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == audit_svc.TASK_REASSIGNED)
        .order_by(AuditEvent.created_at.desc()).limit(25)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_ops.html",
        {
            "scheduler": state,
            "drive_qsize": drive_qsize,
            "resume_qsize": resume_qsize,
            "timeout_events": timeout_events,
            "reassign_events": reassign_events,
            "current_user_is_admin": True,
        },
    )


# ---------------------------------------------------------------------------
# 模型配置（/admin/model，2026-09-19）
#
# 需求原文：「首页我没有看到配置模型的页面，要加进去。默认就是 deepseek 官网的
# flash 模型，只要给个 apikey 就行。」三个已定裁定：存库 + 立即生效；仅管理员可改。
#
# 为什么每个探测失败码都配一句中文：错误码是给日志和测试看的，页面是给人看的。
# 「LLM_MODEL_NOT_FOUND」不会告诉任何人下一步该做什么。
# ---------------------------------------------------------------------------

_PROBE_MESSAGES = {
    "ok": "连通正常：上游接受了这把 Key 和这个模型 id。",
    "LLM_NOT_CONFIGURED": "尚未配置 API Key：填入后保存，或确认服务端已设 DEEPSEEK_API_KEY。",
    "LLM_AUTH_FAILED": "API Key 被上游拒绝（401/403）。请确认 Key 正确、未过期、未泄露后重置。",
    "LLM_MODEL_NOT_FOUND": "上游不认这个模型 id。请改回 deepseek-flash 后重试。",
    "LLM_TIMEOUT": "请求超时。多为网络或上游不可达，与 Key 无关。",
    "LLM_NETWORK_ERROR": "网络不通：本机联不出去（DNS / 出网策略 / 代理）。",
    "LLM_HTTP_ERROR": "上游返回非 200。常见原因：账户余额不足、或被限流。",
    "LLM_UNKNOWN_ERROR": "探测过程中出现未预期错误，详见服务端日志。",
}


def _model_context(request: Request, db: Session, settings: Settings, *,
                   error: str | None = None, saved: bool = False,
                   probe_code: str = "") -> dict:
    row = read_config(db)
    effective = resolve_effective(db, settings)
    updated_by = None
    if row is not None and row.updated_by is not None:
        actor = db.get(User, row.updated_by)
        updated_by = actor.username if actor is not None else None
    return {
        "effective": effective,
        "row": row,
        "masked_key": mask_secret(effective.api_key),
        "models": ALLOWED_MODELS,
        "retired_models": RETIRED_MODELS,
        "default_base_url": DEFAULT_BASE_URL,
        "env_base_url": settings.llm_base_url,
        "env_model": settings.llm_model,
        "key_console_url": API_KEY_CONSOLE_URL,
        "updated_by": updated_by,
        "error": error,
        "saved": saved,
        # 未指定 test 参数时 probe_code 为空：既不显示成功也不显示失败。
        "probe_code": probe_code,
        "probe_ok": probe_code == "ok",
        "probe_message": _PROBE_MESSAGES.get(probe_code, ""),
        "current_user_is_admin": True,
    }


def _invalidate_llm(request: Request) -> None:
    """让 app 上的 LLM 门面丢弃缓存，下一次调用即回读 DB。

    带 getattr 双层保护：测试里 app.state.llm 可能是 FakeLLM / None，
    缺一个 invalidate 不应该让保存动作失败。
    """
    invalidate = getattr(getattr(request.app.state, "llm", None), "invalidate", None)
    if callable(invalidate):
        invalidate()


@router.get("/admin/model", response_class=HTMLResponse)
def model_config_page(
    request: Request,
    saved: str = "",
    test: str = "",
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request, "model_config.html",
        _model_context(request, db, settings, saved=bool(saved), probe_code=test),
    )


@router.post("/admin/model", response_class=HTMLResponse)
def model_config_save(
    request: Request,
    model: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    clear_api_key: str | None = Form(None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_csrf_admin),
    settings: Settings = Depends(get_settings),
):
    try:
        row, changed = save_config(
            db, actor_id=admin.id, model=model, base_url=base_url,
            api_key=api_key, clear_api_key=bool(clear_api_key),
        )
    except ConfigValidationError as exc:
        # 校验失败不该落任何东西：回滚后原样重画页面并把原因写清楚。
        db.rollback()
        return templates.TemplateResponse(
            request, "model_config.html",
            _model_context(request, db, settings, error=str(exc)),
            status_code=422,
        )
    audit_svc.record_audit(
        db, audit_svc.LLM_CONFIG_UPDATED, actor_user_id=admin.id,
        detail={"model": row.model, "base_url": row.base_url, "changed": changed},
    )
    db.commit()
    # commit 之后才失效缓存：反过来的话，写入失败也会把缓存清掉，
    # 下一个调用白读一次库，而且会掩盖「其实没改成」。
    _invalidate_llm(request)
    return RedirectResponse("/admin/model?saved=1", status_code=303)


@router.post("/admin/model/test")
def model_config_test(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_csrf_admin),
):
    """用**已保存**的配置发一条最小请求。结果经 303 回页面自解释。

    刻意不做「测试未保存的表单值」：那需要在同一个请求里既存又用，会让
    「保存」和「测试」的语义互相污染。页面上的说明就写「先保存，再测试」。
    """
    llm = getattr(request.app.state, "llm", None)
    code = "LLM_UNKNOWN_ERROR"
    if llm is not None:
        try:
            llm.probe()
            code = "ok"
        except LLMError as exc:
            code = exc.error_code
        except Exception:  # noqa: BLE001
            logger.exception("模型配置探测失败")
    return RedirectResponse(f"/admin/model?test={code}", status_code=303)
