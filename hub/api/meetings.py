"""会议模式 API：voice-copilot 实时协作。

核心流程：
1. 创建会议（关联到 Matter）
2. 参与人提交立场（本地 LLM 整理后的文本）
3. 检测收敛条件（全员提交 OR 超时）
4. 云端 LLM 生成收敛摘要
5. 返回摘要给各参与人

Token 节省策略：
- 只传本地 LLM 整理后的文本，不传原始转写和检索结果
- 云端 LLM 只输出精简 JSON（共识/分歧/追问，每项最多3条）
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hub.api.errors import ApiError
from hub.db.models import (
    Matter,
    MatterParticipant,
    Meeting,
    MeetingConvergence,
    MeetingStance,
    User,
    new_id,
)
from hub.domain.timeutil import utcnow
from hub.llm.runtime import RuntimeLlm
from hub.web.deps import get_current_user, get_db

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


# ========== 依赖注入 ==========

def get_llm(request: Request) -> RuntimeLlm:
    """从 app.state 获取 LLM 实例。

    ⚠ 注解不能去掉：``request`` 缺类型注解时 FastAPI 会把它当成必填
    query 参数（``loc: ["query","request"]``），导致本依赖的端点一律 422。
    """
    return request.app.state.llm


# ========== Pydantic 模型 ==========

class MeetingCreate(BaseModel):
    matter_id: str
    timeout_minutes: int = 3


class MeetingResponse(BaseModel):
    meeting_id: str
    matter_id: str
    round_number: int
    status: str
    timeout_minutes: int


class StanceSubmit(BaseModel):
    text: str
    round: int


class StanceResponse(BaseModel):
    stance_id: str
    converged: bool
    summary: dict | None = None
    waiting_for: list[str] | None = None


class SummaryResponse(BaseModel):
    ready: bool
    round: int | None = None
    summary: dict | None = None


class MeetingBrief(BaseModel):
    """会议列表项。前端列表页用，字段取最小集。"""

    meeting_id: str
    matter_id: str
    matter_title: str | None = None
    round_number: int
    status: str
    timeout_minutes: int
    created_at: str
    started_at: str | None = None
    # 当前轮已提交立场人数，前端显示「2/3 已发言」
    submitted_count: int = 0
    # 应提交人数（参与人 + 视情况含发起人）
    expected_count: int = 0


class MeetingStanceOut(BaseModel):
    stance_id: str
    user_id: int
    username: str
    round_number: int
    text: str
    submitted_at: str


class MeetingDetail(BaseModel):
    """会议详情：含当前轮立场、参与者与提交状态、会议主题。

    前端「进入会话」一次性拉全，避免再发四五个请求。
    """

    meeting_id: str
    matter_id: str
    matter_title: str | None = None
    matter_background: str | None = None
    round_number: int
    status: str
    timeout_minutes: int
    created_at: str
    started_at: str | None = None
    # 当前轮立场
    stances: list[MeetingStanceOut] = []
    # 全部参与人（含是否已提交当前轮立场）
    participants: list[dict] = []
    # 当前用户是否已提交当前轮立场——前端据此决定禁用提交框
    self_submitted: bool = False
    # 尚未提交当前轮立场的用户名，前端显示「等待中」
    waiting_for: list[str] = []


# ========== 辅助函数 ==========

def _require_matter_member(session: Session, matter_id: str, user: User) -> Matter:
    """验证用户是事项成员（发起人或参与人）"""
    matter = session.get(Matter, matter_id)
    if not matter:
        raise ApiError(404, "MATTER_NOT_FOUND", "事项不存在")
    
    # 检查是否是发起人或参与人
    is_initiator = matter.initiator_id == user.id
    is_participant = session.execute(
        select(MatterParticipant)
        .where(MatterParticipant.matter_id == matter_id)
        .where(MatterParticipant.user_id == user.id)
    ).scalar_one_or_none() is not None
    
    if not is_initiator and not is_participant:
        raise ApiError(403, "FORBIDDEN", "您不是该事项的成员")
    
    return matter


async def _check_and_converge(
    session: Session,
    meeting: Meeting,
    llm: RuntimeLlm
) -> MeetingConvergence | None:
    """检查是否满足收敛条件并执行收敛
    
    收敛条件：
    1. 全员提交立场
    2. OR 超时（从首次提交开始计时）
    
    Returns:
        收敛结果，或 None（未满足条件）
    """
    # 获取当前轮次的所有立场
    stances = session.execute(
        select(MeetingStance)
        .where(MeetingStance.meeting_id == meeting.id)
        .where(MeetingStance.round_number == meeting.round_number)
    ).scalars().all()
    
    if not stances:
        return None
    
    # 获取参与人总数
    matter = session.get(Matter, meeting.matter_id)
    participants_count = session.execute(
        select(func.count(MatterParticipant.user_id))
        .where(MatterParticipant.matter_id == meeting.matter_id)
    ).scalar()
    
    # 如果发起人也参与作答，需要计入
    if matter.initiator_participates:
        participants_count += 1
    
    # 已提交的用户数
    submitted_users = {s.user_id for s in stances}
    
    # 检查超时
    first_submit_time = min(s.submitted_at for s in stances)
    timeout_delta = timedelta(minutes=meeting.timeout_minutes)
    is_timeout = utcnow() - first_submit_time > timeout_delta
    
    # 收敛条件
    should_converge = len(submitted_users) >= participants_count or is_timeout
    
    if not should_converge:
        return None
    
    # 更新会议开始时间
    if not meeting.started_at:
        meeting.started_at = first_submit_time
    
    # 调用 LLM 收敛
    convergence = await _do_convergence(session, meeting, stances, llm)
    
    # 进入下一轮
    meeting.round_number += 1
    session.commit()
    
    return convergence


async def _do_convergence(
    session: Session,
    meeting: Meeting,
    stances: list[MeetingStance],
    llm: RuntimeLlm
) -> MeetingConvergence:
    """执行收敛：调用 LLM 生成摘要"""
    matter = session.get(Matter, meeting.matter_id)
    
    # 构建提示词（精简版，省 token）
    prompt = f"""会议主题：{matter.title}

会议背景：{matter.background[:200] if matter.background else '无'}

本轮发言：
"""
    
    for stance in stances:
        user = session.get(User, stance.user_id)
        prompt += f"\n【{user.username}】\n{stance.text}\n"
    
    prompt += """
请分析并返回 JSON：
{
  "consensus": ["共识点（所有人都同意）"],
  "divergences": ["分歧点（观点不一致）"],
  "follow_ups": ["追问（推进讨论的问题）"]
}

要求：
1. 每项最多3条，精简表达
2. 只提取关键内容
3. 追问要具体可回答
4. 直接返回 JSON，不要额外说明
"""
    
    # 调用 LLM
    try:
        response = await llm.generate(prompt, max_tokens=500)
        
        # 解析 JSON
        import json
        import re
        
        # 尝试从响应中提取 JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            summary = json.loads(json_match.group(0))
        else:
            # 解析失败，使用默认结构
            summary = {
                "consensus": ["（LLM 输出解析失败）"],
                "divergences": [],
                "follow_ups": []
            }
    except Exception as e:
        # LLM 调用失败
        summary = {
            "consensus": [],
            "divergences": [],
            "follow_ups": [f"（收敛失败: {str(e)}）"]
        }
    
    # 保存收敛结果
    convergence = MeetingConvergence(
        id=new_id("mcv"),
        meeting_id=meeting.id,
        round_number=meeting.round_number,
        consensus=summary.get("consensus", []),
        divergences=summary.get("divergences", []),
        follow_ups=summary.get("follow_ups", [])
    )
    session.add(convergence)
    session.commit()
    
    return convergence


# ========== API 端点 ==========

@router.post("", response_model=MeetingResponse)
async def create_meeting(
    payload: MeetingCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """创建会议
    
    权限：只有事项发起人可以创建会议
    """
    matter = session.get(Matter, payload.matter_id)
    if not matter:
        raise ApiError(404, "MATTER_NOT_FOUND", "事项不存在")
    
    if matter.initiator_id != current_user.id:
        raise ApiError(403, "FORBIDDEN", "只有发起人可以创建会议")
    
    # 创建会议
    meeting = Meeting(
        id=new_id("mtg"),
        matter_id=payload.matter_id,
        timeout_minutes=payload.timeout_minutes,
        status="active"
    )
    session.add(meeting)
    session.commit()
    
    return MeetingResponse(
        meeting_id=meeting.id,
        matter_id=meeting.matter_id,
        round_number=meeting.round_number,
        status=meeting.status,
        timeout_minutes=meeting.timeout_minutes
    )


@router.post("/{meeting_id}/stances", response_model=StanceResponse)
async def submit_stance(
    meeting_id: str,
    payload: StanceSubmit,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
    llm: RuntimeLlm = Depends(get_llm)
):
    """提交会议立场
    
    参与人提交本地 LLM 整理后的立场文本。
    自动触发收敛检查。
    """
    
    meeting = session.get(Meeting, meeting_id)
    if not meeting:
        raise ApiError(404, "MEETING_NOT_FOUND", "会议不存在")
    
    if meeting.status != "active":
        raise ApiError(400, "MEETING_NOT_ACTIVE", "会议未激活")
    
    # 验证用户是事项成员
    _require_matter_member(session, meeting.matter_id, current_user)
    
    # 检查是否已提交
    existing = session.execute(
        select(MeetingStance)
        .where(MeetingStance.meeting_id == meeting_id)
        .where(MeetingStance.round_number == payload.round)
        .where(MeetingStance.user_id == current_user.id)
    ).scalar_one_or_none()
    
    if existing:
        raise ApiError(409, "ALREADY_SUBMITTED", "本轮已提交立场")
    
    # 保存立场
    stance = MeetingStance(
        id=new_id("mst"),
        meeting_id=meeting_id,
        user_id=current_user.id,
        round_number=payload.round,
        text=payload.text
    )
    session.add(stance)
    session.commit()
    
    # 检查收敛（需要 LLM 实例）
    convergence = None
    if llm:
        try:
            convergence = await _check_and_converge(session, meeting, llm)
        except Exception as e:
            # 收敛失败不影响立场提交
            print(f"Convergence error: {e}")
    
    if convergence:
        return StanceResponse(
            stance_id=stance.id,
            converged=True,
            summary={
                "consensus": convergence.consensus,
                "divergences": convergence.divergences,
                "follow_ups": convergence.follow_ups
            }
        )
    else:
        # 获取等待的用户（meeting.matter_id 直连 matter_participants，
        # 不需要先把 Matter 读出来）
        all_participants = session.execute(
            select(User.username)
            .join(MatterParticipant, MatterParticipant.user_id == User.id)
            .where(MatterParticipant.matter_id == meeting.matter_id)
        ).scalars().all()
        
        submitted = session.execute(
            select(User.username)
            .join(MeetingStance, MeetingStance.user_id == User.id)
            .where(MeetingStance.meeting_id == meeting_id)
            .where(MeetingStance.round_number == meeting.round_number)
        ).scalars().all()
        
        waiting_for = [u for u in all_participants if u not in submitted]
        
        return StanceResponse(
            stance_id=stance.id,
            converged=False,
            waiting_for=waiting_for
        )


@router.get("/{meeting_id}/summary", response_model=SummaryResponse)
async def get_summary(
    meeting_id: str,
    round: int | None = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """获取会议摘要
    
    用于本地轮询：定期检查收敛是否完成。
    """
    meeting = session.get(Meeting, meeting_id)
    if not meeting:
        raise ApiError(404, "MEETING_NOT_FOUND", "会议不存在")
    
    # 验证权限
    _require_matter_member(session, meeting.matter_id, current_user)
    
    # 获取指定轮次的收敛结果
    target_round = round if round is not None else meeting.round_number - 1
    
    convergence = session.execute(
        select(MeetingConvergence)
        .where(MeetingConvergence.meeting_id == meeting_id)
        .where(MeetingConvergence.round_number == target_round)
    ).scalar_one_or_none()
    
    if not convergence:
        return SummaryResponse(ready=False)
    
    return SummaryResponse(
        ready=True,
        round=target_round,
        summary={
            "consensus": convergence.consensus,
            "divergences": convergence.divergences,
            "follow_ups": convergence.follow_ups
        }
    )


# ========== 列表与详情（供 web-ui 前端使用） ==========


def _visible_matter_ids(session: Session, user: User) -> list[str]:
    """当前用户作为发起人**或**参与人的全部事项 id。

    列表接口据此过滤，避免看到别人的会议。
    """
    as_initiator = session.execute(
        select(Matter.id).where(Matter.initiator_id == user.id)
    ).scalars().all()
    as_participant = session.execute(
        select(MatterParticipant.matter_id)
        .where(MatterParticipant.user_id == user.id)
    ).scalars().all()
    return list(set(as_initiator) | set(as_participant))


def _participant_usernames(session: Session, matter_id: str) -> list[str]:
    """事项参与人的用户名列表（不含发起人，除非他也是参与人）。"""
    return list(
        session.execute(
            select(User.username)
            .join(MatterParticipant, MatterParticipant.user_id == User.id)
            .where(MatterParticipant.matter_id == matter_id)
        ).scalars().all()
    )


def _expected_count(session: Session, matter: Matter) -> int:
    """本轮应收到的立场数：参与人数 + 发起人（若他也作答）。

    与 :func:`_check_and_converge` 的收敛判定保持同一套口径。
    """
    count = session.execute(
        select(func.count(MatterParticipant.user_id))
        .where(MatterParticipant.matter_id == matter.id)
    ).scalar() or 0
    if matter.initiator_participates:
        count += 1
    return count


@router.get("", response_model=list[MeetingBrief])
def list_meetings(
    matter_id: str | None = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """会议列表。

    ``matter_id`` 可选：给了返回该事项的会议；不给则返回当前用户作为
    发起人或参与人的全部会议（前端首页用）。
    """
    stmt = None
    if matter_id is not None:
        # 显式指定时先校验权限——不能因为「不在可见集合」就静默返回空列表，
        # 否则越权探测会得到 200+[]，与「事项存在但无权限」无法区分。
        _require_matter_member(session, matter_id, current_user)
        stmt = select(Meeting).where(Meeting.matter_id == matter_id)
    else:
        # 只列出当前用户有权限的会议
        visible_matter_ids = _visible_matter_ids(session, current_user)
        if not visible_matter_ids:
            return []
        stmt = select(Meeting).where(Meeting.matter_id.in_(visible_matter_ids))

    stmt = stmt.order_by(Meeting.created_at.desc())

    result = []
    for meeting in session.scalars(stmt).all():
        matter = session.get(Matter, meeting.matter_id)
        submitted = session.execute(
            select(func.count(func.distinct(MeetingStance.user_id)))
            .where(MeetingStance.meeting_id == meeting.id)
            .where(MeetingStance.round_number == meeting.round_number)
        ).scalar() or 0
        result.append(MeetingBrief(
            meeting_id=meeting.id,
            matter_id=meeting.matter_id,
            matter_title=matter.title if matter else None,
            round_number=meeting.round_number,
            status=meeting.status,
            timeout_minutes=meeting.timeout_minutes,
            created_at=meeting.created_at.isoformat(),
            started_at=meeting.started_at.isoformat() if meeting.started_at else None,
            submitted_count=submitted,
            expected_count=_expected_count(session, matter) if matter else 0,
        ))
    return result


@router.get("/{meeting_id}", response_model=MeetingDetail)
def get_meeting(
    meeting_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """会议详情：一次返回主题、当前轮立场、参与者与提交状态。

    路径顺序注意：本路由必须注册在 ``/{meeting_id}/summary`` 之后不冲突，
    且FastAPI 按声明顺序匹配——``/{meeting_id}`` 不会误吞 ``/{meeting_id}/summary``。
    """
    meeting = session.get(Meeting, meeting_id)
    if not meeting:
        raise ApiError(404, "MEETING_NOT_FOUND", "会议不存在")

    # 权限：必须是事项成员
    _require_matter_member(session, meeting.matter_id, current_user)

    matter = session.get(Matter, meeting.matter_id)

    # 当前轮立场
    stances = session.execute(
        select(MeetingStance)
        .where(MeetingStance.meeting_id == meeting_id)
        .where(MeetingStance.round_number == meeting.round_number)
        .order_by(MeetingStance.submitted_at)
    ).scalars().all()

    stance_out = []
    submitted_names = set()
    for stance in stances:
        user = session.get(User, stance.user_id)
        if user:
            submitted_names.add(user.username)
        stance_out.append(MeetingStanceOut(
            stance_id=stance.id,
            user_id=stance.user_id,
            username=user.username if user else f"user-{stance.user_id}",
            round_number=stance.round_number,
            text=stance.text,
            submitted_at=stance.submitted_at.isoformat(),
        ))

    all_names = _participant_usernames(session, meeting.matter_id)
    # 发起人若参与作答也要计入
    if matter is not None and matter.initiator_participates:
        initiator = session.get(User, matter.initiator_id)
        if initiator and initiator.username not in all_names:
            all_names.append(initiator.username)

    participants = [
        {"username": name, "submitted": name in submitted_names}
        for name in all_names
    ]

    return MeetingDetail(
        meeting_id=meeting.id,
        matter_id=meeting.matter_id,
        matter_title=matter.title if matter else None,
        matter_background=matter.background if matter else None,
        round_number=meeting.round_number,
        status=meeting.status,
        timeout_minutes=meeting.timeout_minutes,
        created_at=meeting.created_at.isoformat(),
        started_at=meeting.started_at.isoformat() if meeting.started_at else None,
        stances=stance_out,
        participants=participants,
        self_submitted=current_user.username in submitted_names,
        waiting_for=[n for n in all_names if n not in submitted_names],
    )
