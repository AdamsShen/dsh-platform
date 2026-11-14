"""执行池 worker：领任务 → 执行 → 心跳。

槽位是协程级的：每个槽位一个循环，`execute()` 内部把阻塞的 DB / provider 调用
放到线程或子进程，不占事件循环。槽位数由 M0 容量报告反推（见 AO_EXEC_SLOTS）。
"""

from __future__ import annotations

import asyncio
import logging
import socket

from ao.executor import execute
from ao.queue import claim_next, heartbeat
from ao.settings import settings

logger = logging.getLogger(__name__)
HOST = socket.gethostname()


class Worker:
    def __init__(self, *, worker_id: str, slots: int = 1, pool: str = "execution") -> None:
        self.worker_id = worker_id
        self.slots = max(1, slots)
        self.pool = pool

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(stop))
        loops = [asyncio.create_task(self._loop(stop, index)) for index in range(self.slots)]
        try:
            await stop.wait()
        finally:
            for task in loops:
                task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(*loops, heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(
                    heartbeat,
                    self.worker_id,
                    host=HOST,
                    pool=self.pool,
                    slots=self.slots,
                )
            except Exception:  # noqa: BLE001 — 心跳失败不能打死 worker
                logger.exception("心跳写入失败: %s", self.worker_id)
            await asyncio.sleep(10)

    async def _loop(self, stop: asyncio.Event, index: int) -> None:
        while not stop.is_set():
            try:
                run = await asyncio.to_thread(claim_next, self.worker_id)
            except Exception:  # noqa: BLE001
                logger.exception("领取任务失败: %s#%s", self.worker_id, index)
                await asyncio.sleep(1)
                continue
            if run is None:
                await asyncio.sleep(0.4)
                continue
            logger.info("领取 run: %s worker=%s", run["id"], self.worker_id)
            try:
                await execute(str(run["id"]))
            except Exception:  # noqa: BLE001 — 单个 run 的异常不影响槽位
                logger.exception("run 执行失败: %s", run["id"])


def make_worker(index: int = 0, slots: int | None = None) -> Worker:
    return Worker(
        worker_id=f"{HOST}-{index}",
        slots=slots if slots is not None else settings.exec_slots,
    )
