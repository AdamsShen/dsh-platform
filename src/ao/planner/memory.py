"""创作会话的记忆：结构化槽位 + 滚动摘要。

依据 `docs/research/2026-11-13-planner-memory.md` 的三层做法：
最近原文保指代、槽位做结构化真值、更早对话压成摘要。
这里只做纯函数，读写落在 `session.py`，方便单测与录制回放。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

SLOT_KEYS = ("subject", "frequency", "outputs", "systems", "constraints", "documents")
MAX_VALUES_PER_SLOT = 20
_EXCERPT_CHARS = 60

# 需求采集阶段的固定槽位（设计 §7.4 第 1 步）：主体、频率、产物、外部系统、约束
_PATTERNS: dict[str, re.Pattern[str]] = {
    "subject": re.compile(r"(?:给|对|针对)([\u4e00-\u9fa5A-Za-z0-9_]{1,20})"),
    "frequency": re.compile(
        r"(?:每天|每日|每周[一二三四五六日天]?|每个?工作日|每小时|每\d+\s*分钟"
        r"|cron[:：]?\s*[^\s，。；]+|\d{1,2}[:：]\d{2})"
    ),
    "outputs": re.compile(
        r"(?:日报|周报|月报|报告|邮件|表格|excel|xlsx|csv|markdown|pdf|json"
        r"|飞书文档|钉钉文档|Excel)",
        re.IGNORECASE,
    ),
    "systems": re.compile(
        r"(?:mcp__[a-z0-9_]+|钉钉|飞书|企业微信|Slack|GitHub|Jira|日历|邮箱|Notion)",
        re.IGNORECASE,
    ),
    "constraints": re.compile(
        r"(?:不超过|最多|至少|禁止|不要|必须|仅限|不得超过|上限|超时[^\s，。；]{0,10})"
    ),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def extract_slots(text: str) -> dict[str, list[str]]:
    """从一段用户/助手文本里抽槽位值；规则式，不调模型（确定性优先）。"""
    found: dict[str, list[str]] = {}
    for key, pattern in _PATTERNS.items():
        values: list[str] = []
        for match in pattern.finditer(text or ""):
            value = (match.group(1) if match.lastindex else match.group(0)).strip()
            if value and value not in values:
                values.append(value)
        if values:
            found[key] = values
    return found


def merge_slots(
    current: dict[str, Any] | None,
    extracted: dict[str, list[str]],
    *,
    source: str,
    at: str | None = None,
) -> dict[str, Any]:
    """把新值并进槽位；同值只保留第一次来源，避免 20 轮后同一值出现 20 次。"""
    merged: dict[str, list[dict[str, Any]]] = {
        key: list(values or []) for key, values in (current or {}).items()
    }
    stamp = at or _now_iso()
    for key, values in extracted.items():
        bucket = merged.setdefault(key, [])
        for value in values:
            if any(str(item.get("value")) == value for item in bucket if isinstance(item, dict)):
                continue
            bucket.append({"value": value, "source": source, "at": stamp})
        if len(bucket) > MAX_VALUES_PER_SLOT:
            # 超限丢最早的值：最近说的更可信
            merged[key] = bucket[-MAX_VALUES_PER_SLOT:]
    return merged


def slot_values(slots: dict[str, Any] | None, key: str) -> list[str]:
    bucket = (slots or {}).get(key) or []
    return [str(item.get("value")) for item in bucket if isinstance(item, dict)]


def render_slots(slots: dict[str, Any] | None) -> str:
    """给模型看的槽位快照；空槽位显式写"未提供"，逼模型追问而不是自己编。"""
    lines = []
    for key in SLOT_KEYS:
        values = slot_values(slots, key)
        lines.append(f"- {key}: {'、'.join(values) if values else '未提供'}")
    return "\n".join(lines)


def summary_digest(messages: list[dict[str, Any]], slots: dict[str, Any] | None) -> str:
    """把一批消息压成固定格式摘要；确定性文本，不依赖模型输出。"""
    lines: list[str] = []
    for message in messages:
        content = " ".join(str(message.get("content") or "").split())
        if not content:
            continue
        lines.append(f"{message.get('role')}: {content[:_EXCERPT_CHARS]}")
    slot_line = "；".join(
        f"{key}={'、'.join(slot_values(slots, key))}"
        for key in SLOT_KEYS
        if slot_values(slots, key)
    )
    digest = "对话摘要：\n" + "\n".join(lines)
    if slot_line:
        digest += f"\n当时的槽位：{slot_line}"
    return digest
