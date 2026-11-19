"""生成期页面 Schema（P4-T09 / M4a-W47）。

七步生成的第 ④ 步要的是"运行监控页"，不是让模型自由发挥 UI：这里的 Schema
完全由工作流元信息推出，确定性可复现，模型只在第 ② 步决定节点清单。

另外提供 `smoke_page`：编辑器/编译器允许的组件未必都有渲染器（如 chart/modal
目前只有契约没有实现），提前在生成期挡住，比发布后让用户看到"不支持的组件"好。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 契约里允许、但编译器还没写渲染分支的组件。新增渲染分支时同步删掉对应项
UNRENDERED = frozenset({"chart", "modal", "page"})


def generate_page(*, workflow_id: str, title: str, requirement: str = "") -> dict[str, Any]:
    """按工作流元信息推导一版运行看板 Schema（筛选 + 表格 + 详情 + 事件）。"""
    page_title = (title or "运行看板").strip()[:40]
    return {
        "page_schema_version": 1,
        "id": "main",
        "title": page_title,
        "layout": "stack",
        "theme": {"primary": "#2f6fed", "background": "#f7f8fa"},
        "data": {
            "runs": {"source": "run.list", "limit": 50},
            "run": {"source": "run.get", "params": {"id": "$selection.id"}},
            "events": {"source": "run.events", "params": {"id": "$selection.id"}},
        },
        "blocks": [
            {
                "type": "filter",
                "fields": [
                    {
                        "name": "status",
                        "label": "状态",
                        "kind": "select",
                        "options": [
                            {"label": "全部", "value": ""},
                            {"label": "运行中", "value": "running"},
                            {"label": "成功", "value": "succeeded"},
                            {"label": "失败", "value": "failed"},
                        ],
                    }
                ],
            },
            {
                "type": "table",
                "title": "运行记录",
                "source": "runs",
                "pageSize": 20,
                "columns": [
                    {"field": "id", "label": "ID", "format": "text"},
                    {"field": "status", "label": "状态", "format": "status"},
                    {"field": "created_at", "label": "创建时间", "format": "date"},
                    {"field": "spent_usd", "label": "花费", "format": "number"},
                ],
                "onRowClick": {"source": "run", "param": "id"},
            },
            {"type": "detail", "title": "运行详情", "source": "run"},
            {
                "type": "timeline",
                "title": "事件流",
                "source": "events",
                "titleField": "type",
                "timeField": "created_at",
            },
        ],
    }


def smoke_page(root: Path, page_id: str = "main") -> list[dict[str, Any]]:
    """页面冒烟：产物必须存在，且用到的组件都必须有渲染分支。

    返回可直接塞进 `GenerationError.issues` 的列表；空列表表示通过。
    """
    issues: list[dict[str, Any]] = []
    compiled = root / "ui" / "compiled" / page_id
    for name in ("index.html", "app.js", "theme.css"):
        if not (compiled / name).exists():
            issues.append(
                {"code": "E_PAGE_SMOKE", "path": f"ui/compiled/{page_id}/{name}",
                 "message": f"编译产物缺失: {name}"}
            )
    if issues:
        return issues

    schema_path = root / "ui" / "pages" / f"{page_id}.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [{"code": "E_PAGE_SMOKE", "path": str(schema_path), "message": str(exc)}]

    missing = sorted(_used_components(schema) & UNRENDERED)
    if missing:
        issues.append(
            {
                "code": "E_PAGE_SMOKE",
                "path": "blocks",
                "message": "缺少组件渲染: " + "、".join(missing),
                "components": missing,
            }
        )
    return issues


def _used_components(node: Any) -> set[str]:
    """递归收集用到的组件类型（含 tabs/modal 内层）。"""
    used: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("type"), str) and "blocks" not in node:
            used.add(node["type"])
        for value in node.values():
            used |= _used_components(value)
    elif isinstance(node, list):
        for item in node:
            used |= _used_components(item)
    return used



