"""W49：分享态投影——去按钮、去成本字段，原 Schema 不被改。"""

from __future__ import annotations

import copy
import json

from ao.pages import share_view
from ao.pages.generate import generate_page


def _schema() -> dict:
    return json.loads(json.dumps(generate_page(workflow_id="wf_x", title="看板")))


def test_projection_removes_write_blocks_and_cost_columns() -> None:
    schema = _schema()
    schema["blocks"].append(
        {"type": "button", "label": "取消", "action": {"action": "run.cancel"}}
    )
    projected = share_view.project(schema)

    types = [block["type"] for block in projected["blocks"]]
    assert "button" not in types
    table = next(block for block in projected["blocks"] if block["type"] == "table")
    fields = [col["field"] for col in table["columns"]]
    assert "spent_usd" not in fields
    assert "id" in fields
    # 原始 Schema 不动：投影只服务渲染
    assert "spent_usd" in json.dumps(schema, ensure_ascii=False)
    assert "button" in [block["type"] for block in schema["blocks"]]


def test_projection_drops_table_made_only_of_cost_columns() -> None:
    schema = _schema()
    schema["blocks"] = [
        {
            "type": "table",
            "title": "费用",
            "source": "runs",
            "columns": [{"field": "spent_usd", "label": "花费", "format": "number"}],
        },
        {"type": "detail", "title": "详情", "source": "run"},
    ]
    projected = share_view.project(copy.deepcopy(schema))
    assert [block["type"] for block in projected["blocks"]] == ["detail"]


def test_projection_strips_actions_from_kept_blocks() -> None:
    schema = _schema()
    schema["blocks"].append(
        {"type": "text", "text": "说明", "action": {"action": "run.trigger"}}
    )
    projected = share_view.project(schema)
    text_block = next(block for block in projected["blocks"] if block["type"] == "text")
    assert "action" not in text_block
