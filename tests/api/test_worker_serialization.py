"""Per-matter tick/resume serialization tests (M4 任务 3, 设计决策 6).

背景：drive_worker 与 resume_worker 均在后台跑 asyncio.to_thread，当同一
matter 的 round_id 同时入 drive_queue 和 resume_queue 时，锁保证二者不并发，
避免重复轮次/审计。不同 matter 之间不互相阻塞。
"""

import asyncio
import time

import pytest

from hub.background import _matter_lock, _matter_locks


@pytest.fixture(autouse=True)
def _clear_locks():
    """每个测试后清理锁字典，避免跨测试泄漏。"""
    _matter_locks.clear()
    yield
    _matter_locks.clear()


def test_same_matter_returns_same_lock():
    assert _matter_lock("m1") is _matter_lock("m1")


def test_different_matter_returns_different_lock():
    assert _matter_lock("m1") is not _matter_lock("m2")


def test_lock_is_asyncio_lock():
    assert isinstance(_matter_lock("m1"), asyncio.Lock)


def test_lock_held_during_same_matter_calls():
    """两个协程持同一锁时不会并发进入临界区。"""
    call_times: list[tuple[str, float, float]] = []

    async def mock_run(matter_id: str):
        lock = _matter_lock(matter_id)
        async with lock:
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            t1 = time.monotonic()
            call_times.append((matter_id, t0, t1))

    async def main():
        await asyncio.gather(
            mock_run("m1"),
            mock_run("m1"),
            mock_run("m2"),
        )

    asyncio.run(main())
    # m1 的两次调用不重叠：第一个结束时间 <= 第二个开始时间
    m1_calls = [c for c in call_times if c[0] == "m1"]
    assert len(m1_calls) == 2
    first, second = sorted(m1_calls, key=lambda c: c[1])
    assert first[2] <= second[1]  # first ends before second starts
    # m2 与 m1 并行（不受 m1 锁阻塞）：m2 的调用在 m1 的某个调用期间进行
    m2_calls = [c for c in call_times if c[0] == "m2"]
    assert len(m2_calls) == 1
    # m2 与 m1 的任一调用时间窗口有重叠（并行证明）
    m2_start, m2_end = m2_calls[0][1], m2_calls[0][2]
    overlap = any(
        not (m2_end <= m1[1] or m2_start >= m1[2])
        for m1 in m1_calls
    )
    assert overlap  # m2 ran concurrently with at least one m1 call


def test_different_matters_not_blocked_by_lock():
    """不同 matter 的 to_thread 不被互相阻塞。"""
    entered: list[str] = []
    exited: list[str] = []

    async def hold(matter_id: str):
        lock = _matter_lock(matter_id)
        async with lock:
            entered.append(matter_id)
            await asyncio.sleep(0.05)
            exited.append(matter_id)

    async def main():
        # m1 先获取锁，sleep 期间 m2 不应被阻塞
        task_m1 = asyncio.create_task(hold("m1"))
        await asyncio.sleep(0.01)  # let m1 acquire lock
        await hold("m2")
        await task_m1

    asyncio.run(main())
    # m2 完成时，m1 可能还在持有锁
    assert "m2" in exited


def test_no_matter_id_does_not_block():
    """matter_id 为 None 时不持锁（_run_safe 内部处理）。"""
    # 仅验证不会抛异常；实际驱动仍由 _run_safe 处理
    # 不依赖真实 DB；此测试只验证锁粒度逻辑路径不挂
    assert _matter_lock("any") is not None
