"""调度器：租约回收 + 失败重试扫描。

单实例运行（PG advisory lock）；接管触发条件只有一个：`lease_until < now()`。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from ao.db import read, tx
from ao.queue import reclaim_expired
from ao.triggers import fire_due

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x4155544F  # "AUTO"


def try_lock() -> bool:
    with read() as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (ADVISORY_LOCK_KEY,)
        ).fetchone()
    return bool(row and row["ok"])


def reap_once() -> list[dict]:
    reclaimed = reclaim_expired()
    if reclaimed:
        logger.warning("回收过期租约 %s 个: %s", len(reclaimed), [r["id"] for r in reclaimed])
    with tx() as conn:
        rows = conn.execute(
            "UPDATE runs SET status='queued', lease_owner=NULL, lease_until=NULL,"
            " epoch = epoch + 1"
            " WHERE status='failed' AND retry_count < 3"
            "   AND ended_at < now() - interval '5 minutes'"
            " RETURNING id"
        ).fetchall()
    if rows:
        logger.info("定时重试 %s 个平台缺陷失败 run", len(rows))

    # 定时触发：与租约回收同一循环，单实例 advisory lock 保证只有一个调度器在推进
    fired = fire_due()
    if fired:
        logger.info(
            "定时触发 %s 个计划: %s",
            len(fired),
            [(item["trigger_id"], item["status"]) for item in fired],
        )
    return reclaimed


async def sync_gateway_spend() -> int:
    """把网关流水同步进 `llm_usage`；未启用网关时是空操作。

    始终拉「昨天到今天」：spend log 落库有 2-4s 延迟，跨天边界上只拉今天会漏。
    重复行由 `uq_llm_usage_gateway_request` 去重，多拉无害。
    """
    from ao.settings import settings

    if not settings.gateway_enabled or settings.mock:
        return 0
    from ao.gateway import sync_spend_logs
    from ao.util import utc_now

    today = utc_now()
    start = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    # end_date 按当天 00:00 解析（实测），写成 today 会把今天所有流水排除掉，必须 +1 天
    end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        return await sync_spend_logs(start=start, end=end)
    except Exception:  # noqa: BLE001 — 同步失败不能拖垮租约回收
        logger.exception("网关流水同步失败")
        return 0


async def run_forever(interval: float = 10.0, stop: asyncio.Event | None = None) -> None:
    from ao.settings import settings

    stop = stop or asyncio.Event()
    last_sync: float | None = None
    while not stop.is_set():
        try:
            await asyncio.to_thread(reap_once)
        except Exception:  # noqa: BLE001
            logger.exception("调度扫描失败")
        now = asyncio.get_running_loop().time()
        if last_sync is None or now - last_sync >= settings.gateway_sync_interval:
            inserted = await sync_gateway_spend()
            if inserted:
                logger.info("网关流水同步 %s 条", inserted)
            last_sync = now
        await asyncio.sleep(interval)
