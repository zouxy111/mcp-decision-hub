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
