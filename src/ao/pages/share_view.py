"""分享态页面投影（P4-T11 / M4a-W49）。

分享链接是"给外部看一眼"，不是"给外部一份控制台"：
- 去掉所有写操作（button/form 与 action/submit），避免出现点了必 403 的按钮；
- 去掉费用字段，避免把内部成本结构暴露给被分享人。

投影只作用于**渲染前的 Schema 副本**，不写盘、不改发布物，撤销分享即恢复原样。
"""

from __future__ import annotations

import json
from typing import Any

#: 字段名与中文标签两侧都要判，单靠字段名会漏掉 `cost`/`amount` 这类自定义命名
COST_FIELDS = frozenset({"spent_usd", "cost_usd", "cost", "budget_usd", "amount_usd"})
COST_LABELS = ("花费", "成本", "费用", "预算")
WRITE_BLOCKS = frozenset({"button", "form"})
WRITE_KEYS = ("action", "submit")


def project(schema: dict[str, Any]) -> dict[str, Any]:
    """返回剔除了写操作与费用字段的 Schema 副本。"""
    cloned = json.loads(json.dumps(schema))
    blocks = _filter_blocks(cloned.get("blocks") or [])
    cloned["blocks"] = blocks
    return cloned


def _filter_blocks(blocks: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in WRITE_BLOCKS:
            continue
        for key in WRITE_KEYS:
            block.pop(key, None)
        if block.get("type") == "table":
            block["columns"] = [
                column
                for column in (block.get("columns") or [])
                if not _is_cost_column(column)
            ]
        for key in ("blocks", "tabs"):
            inner = block.get(key)
            if isinstance(inner, list):
                if key == "tabs":
                    for tab in inner:
                        if isinstance(tab, dict):
                            tab["blocks"] = _filter_blocks(tab.get("blocks") or [])
                else:
                    block[key] = _filter_blocks(inner)
        if block.get("type") == "table" and not block["columns"]:
            # 整张表都是成本列：连表一起去掉，别留一个空壳
            continue
        kept.append(block)
    return kept


def _is_cost_column(column: Any) -> bool:
    if not isinstance(column, dict):
        return True
    field = str(column.get("field") or "")
    label = str(column.get("label") or "")
    if field in COST_FIELDS:
        return True
    return any(word in label for word in COST_LABELS)
