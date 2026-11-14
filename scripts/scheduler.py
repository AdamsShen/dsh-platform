"""启动调度器（单实例）：回收过期租约、定时重试。"""

from __future__ import annotations

import asyncio
import logging
import signal

from ao.db import close_pool
from ao.scheduler import run_forever, try_lock


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not try_lock():
        logging.error("已有 scheduler 在跑（advisory lock 被占用）")
        return 1
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run_forever(stop=stop)
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
