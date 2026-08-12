"""In-process background driver for the round pipeline.

An asyncio.Queue of round_ids plus one worker coroutine. The sync SQLAlchemy
pipeline runs in a worker thread via asyncio.to_thread, so LLM calls never
block HTTP requests (PRD 11.2, constraint 10).

The worker polls with a 1s timeout: sync request handlers enqueue with plain
queue.put_nowait() from worker threads, which does not reliably wake the
event loop. The poll bounds the wakeup delay and avoids cross-thread
loop.call_soon entirely. Deliberate simplification for the single-process
SQLite deployment.
"""

import asyncio
import logging

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
