"""发布期页面编译（P4-T09 的编译半程，M4a-W43 接入发布流水线）。

规则：
- `ui/pages/*.json` 里有 `page_schema_version` 的，按契约校验并编译到 `ui/compiled/<page>/`；
- 没有该字段的是 P2 早期的占位文件（只有节点 id 列表），跳过并在结果里记 `skipped`；
- 任何一份校验失败都**不允许发布**——带病发布等于把错误推迟到用户打开页面时。

编译必须在 `freeze_package` 之前完成，这样 `ui/compiled/` 会进入 `manifest_hash`，
"跑的不是发布的"才能在加载期被发现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ao.pages import compiler
from ao.pages import schema as page_schema


def compile_all_pages(root: Path) -> dict[str, Any]:
    """编译包内全部页面；返回 `{compiled, skipped, issues}`，不抛异常。"""
    pages_dir = root / "ui" / "pages"
    result: dict[str, Any] = {"compiled": [], "skipped": [], "issues": []}
    if not pages_dir.is_dir():
        return result

    for path in sorted(pages_dir.glob("*.json")):
        page_id = path.stem
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result["issues"].append(
                {
                    "code": "E_VERSION",
                    "path": f"ui/pages/{path.name}",
                    "message": f"JSON 解析失败: {exc}",
                }
            )
            continue
        if not isinstance(raw, dict) or "page_schema_version" not in raw:
            result["skipped"].append(page_id)
            continue
        try:
            schema = page_schema.load(raw)
        except page_schema.PageSchemaError as exc:
            for issue in exc.issues:
                result["issues"].append({"page": page_id, **issue.as_dict()})
            continue
        compiler.write_page(schema, root / "ui" / "compiled" / page_id)
        result["compiled"].append(page_id)
    return result
