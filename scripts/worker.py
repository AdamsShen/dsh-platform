"""启动执行池 worker。

用法：
    uv run python scripts/worker.py --slots 4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from ao.db import close_pool
from ao.worker import make_worker


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slots", type=int, default=None)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = make_worker(args.index, args.slots)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await worker.run(stop)
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
