"""W46：编辑器操作——加列/换类型/挂动作，且发布物 hash 不受影响。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ao.pages import editor as page_editor


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "draft"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "manifest.json").write_text('{"id": "wf_edit"}\n', encoding="utf-8")
    (root / "graph.py").write_text("G = None\n", encoding="utf-8")
    return root


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_starter_passes_schema_validation(root: Path) -> None:
    schema = page_editor.load_page(root, "main")
    clean = page_editor.save_page(root, "main", schema)
    assert clean["page_schema_version"] == 1
    assert (root / "ui" / "compiled" / "main" / "index.html").exists()


def test_add_column_and_keep_manifest_untouched(root: Path) -> None:
    manifest_before = _hash(root / "manifest.json")
    graph_before = _hash(root / "graph.py")

    schema = page_editor.load_page(root, "main")
    updated = page_editor.apply_op(
        schema,
        "add_column",
        0,
        {"field": "spent_usd", "label": "花费", "format": "number"},
    )
    page_editor.save_page(root, "main", updated)

    saved = json.loads((root / "ui" / "pages" / "main.json").read_text(encoding="utf-8"))
    fields = [col["field"] for col in saved["blocks"][0]["columns"]]
    assert "spent_usd" in fields
    # 编辑页面不能碰到工作流定义本身，否则一改列就换了版本
    assert _hash(root / "manifest.json") == manifest_before
    assert _hash(root / "graph.py") == graph_before


def test_change_type_keeps_title_and_source(root: Path) -> None:
    schema = page_editor.load_page(root, "main")
    table_index = next(i for i, b in enumerate(schema["blocks"]) if b["type"] == "table")
    updated = page_editor.apply_op(schema, "change_type", table_index, {"value": "detail"})
    block = updated["blocks"][table_index]
    assert block["type"] == "detail"
    assert block["title"] == "运行记录"


def test_add_action_on_button_and_reject_on_table(root: Path) -> None:
    schema = page_editor.load_page(root, "main")
    schema["blocks"].append({"type": "button", "label": "取消"})
    index = len(schema["blocks"]) - 1
    updated = page_editor.apply_op(
        schema, "add_action", index, {"value": "run.cancel", "confirm": "1"}
    )
    assert updated["blocks"][index]["action"] == {"action": "run.cancel", "confirm": True}

    with pytest.raises(page_editor.EditorError, match="表格不支持"):
        page_editor.apply_op(schema, "add_action", 0, {"value": "run.cancel"})


def test_move_duplicate_and_delete(root: Path) -> None:
    schema = page_editor.load_page(root, "main")
    original = [b["type"] for b in schema["blocks"]]
    down = page_editor.apply_op(schema, "down", 0, {})
    assert [b["type"] for b in down["blocks"]] == list(reversed(original))
    with pytest.raises(page_editor.EditorError):
        page_editor.apply_op(schema, "up", 0, {})

    duplicated = page_editor.apply_op(schema, "duplicate", 0, {})
    assert len(duplicated["blocks"]) == len(original) + 1
    removed = page_editor.apply_op(duplicated, "delete", 0, {})
    assert len(removed["blocks"]) == len(original)


def test_save_rejects_invalid_schema_without_writing(root: Path) -> None:
    schema = page_editor.starter("main")
    schema["blocks"] = [{"type": "table", "onclick": "alert(1)"}]
    with pytest.raises(page_editor.EditorError):
        page_editor.save_page(root, "main", schema)
    assert not (root / "ui" / "pages" / "main.json").exists()


def test_unknown_operations_are_rejected(root: Path) -> None:
    schema: dict[str, Any] = page_editor.starter("main")
    with pytest.raises(page_editor.EditorError, match="未知操作"):
        page_editor.apply_op(schema, "run_shell", 0, {})
    with pytest.raises(page_editor.EditorError, match="块序号越界"):
        page_editor.apply_op(schema, "up", 99, {})
