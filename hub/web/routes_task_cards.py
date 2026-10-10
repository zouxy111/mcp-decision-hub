"""任务卡的网页路由（v22，2026-10-10）。

Cookie 会话 + CSRF（浏览器通道）。业务全部转调
:mod:`hub.api.task_cards` 服务层——与 MCP / REST 两个 agent 通道同一
事实源。

页面形态：任务卡详情页承载全部动作（改标准/发布/关卡/交活/验收）；
留言板详情页只放卡片列表入口（见 routes_matters._build_detail）。
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from hub.api import task_cards as cards_svc
from hub.api.errors import ApiError
from hub.db.models import TaskDelivery, User
from hub.domain import task_cards as cards
from hub.web.deps import (
    get_current_user,
    get_db,
    register_csrf_globals,
    require_csrf,
)

router = APIRouter(tags=["task-card-pages"])
templates = Jinja2Templates(directory="hub/web/templates")
register_csrf_globals(templates)


def _card_context(db: Session, card, user: User) -> dict:
    """卡片页渲染上下文：卡 + 交付（解析 JSON 列）+ 权限位。"""
    criteria = cards.parse_criteria(card.acceptance_criteria)
    deliveries = []
    for d in cards_svc.list_deliveries(db, card_id=card.id, user_id=user.id):
        deliveries.append({
            "delivery": d,
            "self_check": cards.parse_self_check(d.self_check),
            "ai_review": cards.parse_ai_review(d.ai_review),
        })
    return {
        "card": card,
        "criteria": criteria,
        "criteria_lines": "\n".join(criteria),
        "deliveries": deliveries,
        "is_publisher": card.publisher_id == user.id,
        "review_verdict_label": {
            "pass": "符合", "gap": "有缺口", "unclear": "待澄清",
        },
        "review_status_label": {
            "pass": "AI 审查：全部符合", "gaps": "AI 审查：存在缺口",
            "failed": "AI 审查未跑成（不影响交付）",
        },
        "error": None,
    }


@router.get("/matters/{matter_id}/cards/{card_id}")
def card_page(
    request: Request,
    matter_id: str,
    card_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    card = cards_svc.get_card(db, card_id=card_id, user_id=user.id)
    if card.matter_id != matter_id:
        # 路径与卡不符：按卡的真实归属重定向，而不是 404 制造困惑
        return RedirectResponse(f"/matters/{card.matter_id}/cards/{card.id}",
                                status_code=303)
    ctx = _card_context(db, card, user)
    ctx["current_user_is_admin"] = user.is_admin
    return templates.TemplateResponse(request, "task_card.html", ctx)


@router.post("/matters/{matter_id}/cards")
def create_card(
    request: Request,
    matter_id: str,
    title: str = Form(""),
    description: str = Form(""),
    criteria_text: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        card = cards_svc.create_card(
            db, matter_id=matter_id, publisher=user, title=title,
            description=description,
            acceptance_criteria=[ln for ln in criteria_text.splitlines()],
        )
        db.commit()
    except ApiError as e:
        db.commit()
        return RedirectResponse(f"/matters/{matter_id}#task-cards",
                                status_code=303)
    return RedirectResponse(f"/matters/{matter_id}/cards/{card.id}",
                            status_code=303)


@router.post("/matters/{matter_id}/cards/{card_id}/update")
def update_card(
    request: Request,
    matter_id: str,
    card_id: str,
    title: str = Form(""),
    description: str = Form(""),
    criteria_text: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        cards_svc.update_card(
            db, card_id=card_id, actor=user, title=title,
            description=description,
            acceptance_criteria=[ln for ln in criteria_text.splitlines()],
        )
        db.commit()
    except ApiError as e:
        db.commit()
    return RedirectResponse(f"/matters/{matter_id}/cards/{card_id}",
                            status_code=303)


@router.post("/matters/{matter_id}/cards/{card_id}/publish")
def publish_card(
    request: Request,
    matter_id: str,
    card_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        cards_svc.publish_card(db, card_id=card_id, actor=user)
        db.commit()
    except ApiError:
        db.commit()
    return RedirectResponse(f"/matters/{matter_id}/cards/{card_id}",
                            status_code=303)


@router.post("/matters/{matter_id}/cards/{card_id}/close")
def close_card(
    request: Request,
    matter_id: str,
    card_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        cards_svc.close_card(db, card_id=card_id, actor=user)
        db.commit()
    except ApiError:
        db.commit()
    return RedirectResponse(f"/matters/{matter_id}/cards/{card_id}",
                            status_code=303)


@router.post("/matters/{matter_id}/cards/{card_id}/deliveries")
def submit_delivery(
    request: Request,
    matter_id: str,
    card_id: str,
    summary: str = Form(""),
    met_criteria: list[str] = Form(default=[]),
    unmet_criteria: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    """网页交活：表单里每条标准勾选符合与否，组装 self_check。

    AI 审查走服务层（同步调用，几秒）。页面如实展示审查结论——软门禁，
    有缺口就回去补，但不会被拦。
    """
    llm = request.app.state.llm
    criteria = set(met_criteria) | set(unmet_criteria)
    self_check = [
        {"criterion": c,
         "met": True if c in met_criteria else False}
        for c in criteria
    ]
    try:
        cards_svc.submit_delivery(
            db, card_id=card_id, user=user, summary=summary,
            self_check=self_check, llm=llm)
        db.commit()
    except ApiError:
        db.commit()
    return RedirectResponse(f"/matters/{matter_id}/cards/{card_id}",
                            status_code=303)


@router.post("/deliveries/{delivery_id}/decide")
def decide_delivery(
    request: Request,
    delivery_id: str,
    decision: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    delivery = db.get(TaskDelivery, delivery_id)
    if delivery is None:
        return RedirectResponse("/dashboard", status_code=303)
    try:
        cards_svc.decide_delivery(
            db, delivery_id=delivery_id, actor=user,
            accept=(decision == "accept"), note=note)
        db.commit()
    except ApiError:
        db.commit()
    return RedirectResponse(f"/matters/{delivery.matter_id}/cards/"
                            f"{delivery.card_id}", status_code=303)
