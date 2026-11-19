"""编辑��� v1 的 Schema 变更逻辑（P4-T07 / M4a-W46）。

分工：模板只负责把"按了哪个块、按了什么"提交上来，契约与写盘都在这里，
这样 UI 改版不会碰到校验规则，校验规则也不会散在 HTML 里。

三条约束：
1. 只改 `ui/pages/<page>.json` 与 `ui/compiled/<page>/`，绝不碰 `manifest.json`/`graph.py`；
2. 每次改完立刻过一遍 `page_schema.load`，非法就不写盘（宁可拒绝也不留半成品）；
3. 页面还是 P2 占位（没有 `page_schema_version`）时，用 starter 起步而不是报错。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ao.pages import compiler
from ao.pages import schema as page_schema

TITLE = "运行看板"


class EditorError(ValueError):
    """可预期的编辑失败：越界、类型不支持、改完不合契约。"""


def starter(page_id: str) -> dict[str, Any]:
    """最小可用看板：筛选 + 表格 + 详情，全部挂在只读数据源上。"""
    return {
        "page_schema_version": 1,
        "id": page_id,
        "title": TITLE,
        "layout": "stack",
        "data": {
            "runs": {"source": "run.list", "limit": 50},
            "run": {"source": "run.get", "params": {"id": "$selection.id"}},
        },
        "blocks": [
            {
                "type": "table",
                "title": "运行记录",
                "source": "runs",
                "columns": [
                    {"field": "id", "label": "ID", "format": "text"},
                    {"field": "status", "label": "状态", "format": "text"},
                ],
                "onRowClick": {"source": "run", "param": "id"},
            },
            {"type": "detail", "title": "详情", "source": "run"},
        ],
    }


def load_page(root: Path, page_id: str) -> dict[str, Any]:
    """读页面真值；占位文件或读坏了都退回 starter（编辑器要能开工）。"""
    path = root / "ui" / "pages" / f"{page_id}.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict) and "page_schema_version" in raw:
            return raw
    return starter(page_id)


def apply_op(
    schema: dict[str, Any], op: str, target: int, payload: dict[str, str]
) -> dict[str, Any]:
    """在 Schema 上执行一次编辑操作，返回新 Schema（不改入参）。"""
    blocks = list(schema.get("blocks") or [])
    if op in {"up", "down", "duplicate", "delete"}:
        if not 0 <= target < len(blocks):
            raise EditorError(f"块序号越界: {target}")
        block = blocks[target]
        if op == "up":
            if target == 0:
                raise EditorError("已经在最前面")
            blocks[target - 1], blocks[target] = blocks[target], blocks[target - 1]
        elif op == "down":
            if target == len(blocks) - 1:
                raise EditorError("已经在最后面")
            blocks[target], blocks[target + 1] = blocks[target + 1], blocks[target]
        elif op == "duplicate":
            blocks.insert(target + 1, json.loads(json.dumps(block)))
        else:
            blocks.pop(target)
        return {**schema, "blocks": blocks}

    if not 0 <= target < len(blocks):
        raise EditorError(f"块序号越界: {target}")
    block = blocks[target]

    if op == "add_column":
        if block.get("type") != "table":
            raise EditorError("只有表格能加列")
        field = (payload.get("field") or "").strip()
        label = (payload.get("label") or "").strip()
        fmt = (payload.get("format") or "text").strip()
        if not field or not label:
            raise EditorError("列名与中文标签都不能为空")
        if fmt not in page_schema.COLUMN_FORMATS:
            raise EditorError(f"不支持的格式: {fmt}")
        columns = list(block.get("columns") or [])
        if any(col.get("field") == field for col in columns):
            raise EditorError(f"列已存在: {field}")
        columns.append({"field": field, "label": label, "format": fmt})
        block["columns"] = columns
    elif op == "change_type":
        new_type = (payload.get("value") or "").strip()
        if new_type not in page_schema.COMPONENTS:
            raise EditorError(f"不支持的组件: {new_type}")
        if new_type == block.get("type"):
            raise EditorError("类型没有变化")
        blocks[target] = _convert(block, new_type)
    elif op == "add_action":
        name = (payload.get("value") or "").strip()
        if name not in page_schema.ACTIONS:
            raise EditorError(f"动作不在白名单: {name}")
        action: dict[str, Any] = {"action": name}
        node = (payload.get("node") or "").strip()
        if name == "node.invoke":
            if not node:
                raise EditorError("node.invoke 必须指定节点")
            action["node"] = node
        if payload.get("confirm"):
            action["confirm"] = True
        if block.get("type") == "table":
            # 表格上的动作挂到行点击之外会没有触发点，这里明确拒绝
            raise EditorError("表格不支持直接挂动作，请先加按钮")
        block["action"] = action
    elif op == "add_block":
        new_type = (payload.get("value") or "").strip()
        blocks.append(_blank(new_type))
    else:
        raise EditorError(f"未知操作: {op}")
    return {**schema, "blocks": blocks}


def apply_text_op(
    schema: dict[str, Any], op: str, payload: dict[str, str]
) -> dict[str, Any]:
    """对话式改动的入口：不带块序号，由内容自己定位（第一张表）。

    UI 编辑要人来指"改哪一块"，一句话改页面则由语义决定目标；两者共用
    `apply_op` 的底层操作，避免出现两套改法。
    """
    blocks = list(schema.get("blocks") or [])
    if op == "add_column":
        index = next(
            (i for i, block in enumerate(blocks) if block.get("type") == "table"), None
        )
        if index is None:
            raise EditorError("当前页面没有表格，加列前先加一张表")
        return apply_op(schema, "add_column", index, payload)
    if op == "remove_column":
        field = (payload.get("field") or "").strip()
        for block in blocks:
            if block.get("type") != "table":
                continue
            columns = [
                col for col in (block.get("columns") or []) if col.get("field") != field
            ]
            if len(columns) == len(block.get("columns") or []):
                continue
            if not columns:
                raise EditorError(f"删掉「{field}」后表格就没有列了")
            block["columns"] = columns
            return {**schema, "blocks": blocks}
        raise EditorError(f"表格里没有这一列: {field}")
    if op == "add_block":
        block = _blank((payload.get("value") or "").strip())
        if payload.get("label"):
            if block["type"] == "button":
                block["label"] = payload["label"]
            else:
                block["title"] = payload["label"]
        action = (payload.get("action") or "").strip()
        if action:
            if action not in page_schema.ACTIONS:
                raise EditorError(f"动作不在白名单: {action}")
            block["action"] = {"action": action}
        blocks.append(block)
        return {**schema, "blocks": blocks}
    raise EditorError(f"不支持的对话式操作: {op}")


def save_page(root: Path, page_id: str, schema: dict[str, Any]) -> dict[str, Any]:
    """校验 + 写盘 + 编译；返回校验归一化后的 Schema。"""
    try:
        clean = page_schema.load(schema)
    except page_schema.PageSchemaError as exc:
        raise EditorError("；".join(issue.message for issue in exc.issues)) from exc
    path = root / "ui" / "pages" / f"{page_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    compiler.write_page(clean, root / "ui" / "compiled" / page_id)
    return clean


def _convert(block: dict[str, Any], new_type: str) -> dict[str, Any]:
    """换组件：保留 title/source 这类通用字段，其余换成新类型的空壳。"""
    fresh = _blank(new_type)
    for key in ("title", "source"):
        if key in block:
            fresh[key] = block[key]
    return fresh


def _blank(component: str) -> dict[str, Any]:
    if component not in page_schema.COMPONENTS:
        raise EditorError(f"不支持的组件: {component}")
    if component == "table":
        return {"type": "table", "title": "新表格", "columns": []}
    if component == "button":
        return {"type": "button", "label": "新按钮"}
    return {"type": component, "title": "新块"}
