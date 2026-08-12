"""Timeout scan service (FR-14b / PRD 10.4).

Pure sync; driven by ``hub.background.timeout_worker``. 扫描完全基于 DB
``deadline_at``（重启自动重建，不依赖内存定时器）。所有状态推进用条件
UPDATE + rowcount 检查，重复扫描天然 no-op（幂等）。置 timeout 后复用
``pipeline.maybe_drive_round`` 评估收齐——``is_round_collected`` 已含
``timeout`` 终态，domain 层零改动。先 commit 再入队（与 M2 submit 路由
纪律一致），避免 worker 读到未提交状态。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update

from hub.api import audit
from hub.api.pipeline import maybe_drive_round
from hub.config import Settings
from hub.db.models import Task
from hub.domain.timeutil import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SchedulerState:
    """进程内调度器运行状态单例（任务 8 在 /admin/ops 展示）。"""

    last_run_at: datetime | None = None  # naive UTC
    last_processed: int = 0
    last_duration_ms: int = 0
    consecutive_failures: int = 0
    total_timeouts: int = 0


SCHEDULER_STATE = SchedulerState()


def scan_once(session_factory, settings: Settings, drive_queue=None) -> int:
    """扫描到期 pending 任务 → timeout + task_timeout 审计；对每个超时任务
    调 ``maybe_drive_round`` 评估收齐；收齐则把 round_id 放入 drive_queue
    （可为 None，测试不传）。返回本次置 timeout 的任务条数。异常上抛
    （由 worker 记状态）。"""
    round_ids_to_drive: list[str] = []
    with session_factory() as session:
        now = utcnow()
        task_rows = session.execute(
            select(Task.id, Task.round_id, Task.matter_id).where(
                Task.status == "pending", Task.deadline_at <= now
            )
        ).all()
        processed = 0
        seen_rounds: set[str] = set()
        for task_id, round_id, matter_id in task_rows:
            result = session.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "pending")
                .values(status="timeout")
            )
            if result.rowcount != 1:
                continue  # 并发路径已处理；重复扫描天然 no-op
            audit.record_audit(
                session, audit.TASK_TIMEOUT, actor_user_id=None,
                matter_id=matter_id,
                detail={"task_id": task_id, "round_id": round_id},
            )
            processed += 1
            driven = maybe_drive_round(session, task_id=task_id)
            if driven is not None and driven not in seen_rounds:
                seen_rounds.add(driven)
                round_ids_to_drive.append(driven)
        session.commit()
    # 先 commit 再入队：worker 读到的永远是已提交状态
    if drive_queue is not None:
        for round_id in round_ids_to_drive:
            drive_queue.put_nowait(round_id)
    return processed
