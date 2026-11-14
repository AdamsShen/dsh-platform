"""cron 表达式：只支持标准 5 段（分 时 日 月 周）。

单独成模块是为了让 manifest 校验和调度器共用同一套规则——
发布期拦住非法的 cron，比等到半夜触发时才炸好得多。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

#: 5 段标准 cron 不会这么长；设上限避免有人把整行配置塞进来
MAX_CRON_LENGTH = 64


def timezone_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知时区: {name!r}") from exc


def validate_cron(cron: str, timezone: str = "Asia/Shanghai") -> None:
    """校验 cron 与时区，非法直接抛 ValueError（发布期就要失败）。"""
    expr = (cron or "").strip()
    if not expr:
        raise ValueError("cron 不能为空")
    if len(expr) > MAX_CRON_LENGTH or len(expr.split()) != 5:
        raise ValueError(f"cron 必须是 5 段表达式（分 时 日 月 周）: {cron!r}")
    timezone_of(timezone)
    # croniter 也接受 6 段（带秒），上面已按段数卡死
    if not croniter.is_valid(expr):
        raise ValueError(f"cron 表达式不合法: {cron!r}")


def next_fire(cron: str, timezone: str, after: datetime) -> datetime:
    """返回 `after` 之后的下一个触发时刻（UTC）。

    在目标时区里算，再转回 UTC 存库：跨时区/夏令时由 croniter + zoneinfo 处理，
    调度循环只需要比较时间戳。
    """
    tz = timezone_of(timezone)
    base = after.astimezone(tz)
    return croniter(cron.strip(), base).get_next(datetime).astimezone(UTC)
