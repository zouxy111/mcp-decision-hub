"""In-process background driver for the round pipeline.

An asyncio.Queue of round_ids plus one worker coroutine. The sync SQLAlchemy
pipeline runs in a worker thread via asyncio.to_thread, so LLM calls never
block HTTP requests (PRD 11.2, constraint 10).

The worker polls with a 1s timeout: sync request handlers enqueue with plain
queue.put_nowait() from worker threads, which does not reliably wake the
event loop. The poll bounds the wakeup delay and avoids cross-thread
loop.call_soon entirely. Deliberate simplification for the single-process
SQLite deployment.

timeout_worker (FR-14b / PRD 10.4): periodic deadline scan. 重建不依赖内存
定时器——lifespan 启动即无条件创建本协程；扫描完全基于 DB ``deadline_at``，
任意时刻重启最坏延迟一个扫描周期即恢复超时判定（场景 13）。先扫后睡，
启动后立即扫一次。
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 1.0


async def drive_worker(queue: asyncio.Queue, session_factory, settings, llm) -> None:
    while True:
        try:
            round_id = await asyncio.wait_for(
                queue.get(), timeout=POLL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            continue
        try:
            await asyncio.to_thread(_run_safe, session_factory, settings,
                                    round_id, llm)
        finally:
            queue.task_done()


def _run_safe(session_factory, settings, round_id, llm) -> None:
    from hub.api.pipeline import run_round_pipeline

    try:
        run_round_pipeline(session_factory, settings, round_id=round_id, llm=llm)
    except Exception:
        logger.exception("round pipeline crashed round_id=%s", round_id)


async def resume_worker(queue: asyncio.Queue, session_factory, settings,
                        llm) -> None:
    """Resolution-gate resume driver. Queue items are (matter_id, action)
    tuples; action in {"continue_probing", "accept", "decide"}."""
    while True:
        try:
            item = await asyncio.wait_for(
                queue.get(), timeout=POLL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            continue
        try:
            await asyncio.to_thread(_resume_safe, session_factory, settings,
                                    item, llm)
        finally:
            queue.task_done()


def _resume_safe(session_factory, settings, item, llm) -> None:
    from hub.graph.matter_graph import resume_matter_gate

    matter_id, action = item
    try:
        resume_matter_gate(session_factory, settings, matter_id=matter_id,
                           action=action, llm=llm)
    except Exception:
        logger.exception("gate resume crashed matter_id=%s action=%s",
                         matter_id, action)


async def timeout_worker(session_factory, settings, drive_queue) -> None:
    """FR-14b: periodic deadline scan. Rebuilt unconditionally at startup;
    scans are DB-driven so restarts never lose timeout detection. 先扫后睡：
    启动后立即扫一次，重启恢复最坏延迟一个周期以内。"""
    while True:
        try:
            await asyncio.to_thread(_scan_safe, session_factory, settings,
                                    drive_queue)
        except Exception:
            logger.exception("timeout scan crashed")
        await asyncio.sleep(settings.timeout_scan_interval_seconds)


def _scan_safe(session_factory, settings, drive_queue) -> int:
    """计时调 scan_once 并更新 SCHEDULER_STATE；异常不抛出（由本函数记状态
    与日志），返回本次处理条数。"""
    from hub.api import scheduler
    from hub.domain.timeutil import utcnow

    state = scheduler.SCHEDULER_STATE
    start = time.monotonic()
    try:
        processed = scheduler.scan_once(session_factory, settings,
                                        drive_queue=drive_queue)
    except Exception:
        logger.exception("timeout scan failed")
        state.last_run_at = utcnow()
        state.last_processed = 0
        state.last_duration_ms = int((time.monotonic() - start) * 1000)
        state.consecutive_failures += 1
        return 0
    state.last_run_at = utcnow()
    state.last_processed = processed
    state.last_duration_ms = int((time.monotonic() - start) * 1000)
    state.consecutive_failures = 0
    state.total_timeouts += processed
    return processed
