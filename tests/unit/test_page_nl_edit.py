"""W49：一句话改页面——只改 Schema，不碰 manifest/graph。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ao.pages import editor as page_editor
from ao.pages import nl_edit


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "draft"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "manifest.json").write_text('{"id": "wf_nl"}\n', encoding="utf-8")
    (root / "graph.py").write_text("G = None\n", encoding="utf-8")
    return root


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_parse_add_column_with_known_label() -> None:
    ops = nl_edit.parse("帮我把表格加一列花费")
    assert ops == [
        {"op": "add_column", "payload": {"field": "spent_usd", "label": "花费", "format": "number"}}
    ]


def test_parse_unknown_label_gets_explicit_fallback_field() -> None:
    ops = nl_edit.parse("表格加一列重试次数")
    assert ops and ops[0]["payload"]["field"].startswith("extra_")
    assert ops[0]["payload"]["label"] == "重试次数"


def test_apply_text_only_touches_page_schema(root: Path) -> None:
    page_editor.save_page(root, "main", page_editor.starter("main"))
    manifest_before = _hash(root / "manifest.json")
    graph_before = _hash(root / "graph.py")

    outcome = nl_edit.apply_text(root, "main", "表格加一列花费")
    assert outcome["ok"], outcome["message"]

    saved = json.loads((root / "ui" / "pages" / "main.json").read_text(encoding="utf-8"))
    fields = [col["field"] for col in saved["blocks"][0]["columns"]]
    assert "spent_usd" in fields
    assert _hash(root / "manifest.json") == manifest_before
    assert _hash(root / "graph.py") == graph_before


def test_remove_column_and_reject_unknown_column(root: Path) -> None:
    page_editor.save_page(root, "main", page_editor.starter("main"))
    assert nl_edit.apply_text(root, "main", "删掉状态列")["ok"]
    saved = json.loads((root / "ui" / "pages" / "main.json").read_text(encoding="utf-8"))
    assert "status" not in [col["field"] for col in saved["blocks"][0]["columns"]]

    failed = nl_edit.apply_text(root, "main", "删掉不存在的列")
    assert not failed["ok"] and "没有这一列" in failed["message"]


def test_unknown_sentence_is_reported_not_guessed(root: Path) -> None:
    outcome = nl_edit.apply_text(root, "main", "把页面改成更好看的风格")
    assert not outcome["ok"]
    assert "没听懂" in outcome["message"]
    assert not (root / "ui" / "pages" / "main.json").exists()
