"""对话式改页面（P4-T08 / M4a-W49）。

规则式解析，不调模型：这类需求（加列/删列/加按钮）语法稳定，走模型只会引入
不确定性；解析不出来的句子明确返回"没听懂"，而不是猜一个改动。

改的是页面 Schema（`ui/pages/<page>.json`），不碰 `manifest.json`/`graph.py`——
页面是给人看的视图，改视图不该等于换工作流版本。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ao.pages import editor as page_editor

#: 常见中文列名 → 运行数据字段；没命中的用 `extra_<hash>` 兜底（明确不是猜语义）
FIELD_HINTS = {
    "花费": ("spent_usd", "number"),
    "成本": ("spent_usd", "number"),
    "费用": ("spent_usd", "number"),
    "状态": ("status", "status"),
    "时间": ("created_at", "date"),
    "创建时间": ("created_at", "date"),
    "版本": ("version", "number"),
    "工作流": ("workflow_id", "text"),
    "触发人": ("trigger_user_id", "text"),
}

_TAIL = r"[：:\s]*([^\s，。；,;]+)"
# 两种中文语序都要认：「加一列花费」与「加花费列」/「删掉状态列」
_ADD_COLUMN_RE = re.compile(r"(?:表格)?(?:里)?(?:加|添加|新增|增加)(?:上)?(?:一)?列" + _TAIL)
_ADD_COLUMN_INLINE_RE = re.compile(r"(?:表格)?(?:里)?(?:加|添加|新增|增加)([^\s，。；,;]{1,8}?)列")
_DROP_COLUMN_RE = re.compile(r"(?:表格)?(?:里)?(?:删|去掉|删除|移除)(?:掉)?(?:一)?列" + _TAIL)
_DROP_COLUMN_INLINE_RE = re.compile(
    r"(?:表格)?(?:里)?(?:删|去掉|删除|移除)(?:掉)?([^\s，。；,;]{1,8}?)列"
)
_ADD_BUTTON_RE = re.compile(r"(?:加|添加|新增)(?:一个)?按钮[：:\s]*([^\s，。；,;]+)")


def parse(text: str) -> list[dict[str, Any]]:
    """把一句话解析成若干 `editor.apply_op` 调用；空列表表示没听懂。"""
    text = (text or "").strip()
    ops: list[dict[str, Any]] = []
    for label in _labels(_ADD_COLUMN_RE, _ADD_COLUMN_INLINE_RE, text):
        field, fmt = _field_for(label)
        ops.append(
            {"op": "add_column", "payload": {"field": field, "label": label, "format": fmt}}
        )
    for label in _labels(_DROP_COLUMN_RE, _DROP_COLUMN_INLINE_RE, text):
        field, _ = _field_for(label)
        ops.append({"op": "remove_column", "payload": {"field": field}})
    for label in _ADD_BUTTON_RE.findall(text):
        ops.append(
            {
                "op": "add_block",
                "payload": {"value": "button", "label": label, "action": "run.cancel"},
            }
        )
    return ops


def apply_text(root: Any, page_id: str, text: str) -> dict[str, Any]:
    """解析并落盘；返回 `{ok, message, ops}`，失败时 `message` 是给人看的原因。"""
    ops = parse(text)
    if not ops:
        return {"ok": False, "message": "没听懂：目前支持“表格加一列花费”“删掉花费列”", "ops": []}
    schema = page_editor.load_page(root, page_id)
    try:
        for item in ops:
            schema = page_editor.apply_text_op(schema, item["op"], item["payload"])
        page_editor.save_page(root, page_id, schema)
    except page_editor.EditorError as exc:
        return {"ok": False, "message": str(exc), "ops": ops}
    return {"ok": True, "message": f"已应用 {len(ops)} 处改动", "ops": ops}


def _labels(primary: re.Pattern[str], inline: re.Pattern[str], text: str) -> list[str]:
    """合并「X列」与「列X」两种语序；去重并丢掉空标签/数量词。"""
    found = primary.findall(text) + inline.findall(text)
    clean: list[str] = []
    for label in found:
        label = label.strip()
        if not label or label in {"一", "个", "这"} or label in clean:
            continue
        clean.append(label)
    return clean


def _field_for(label: str) -> tuple[str, str]:
    if label in FIELD_HINTS:
        return FIELD_HINTS[label]
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:6]
    return f"extra_{digest}", "text"
