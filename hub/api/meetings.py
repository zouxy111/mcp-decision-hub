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

from fastapi import APIRouter, Depends
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

def get_llm(request) -> RuntimeLlm:
    """从 app.state 获取 LLM 实例"""
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
