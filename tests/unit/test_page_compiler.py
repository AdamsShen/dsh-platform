"""W42：编译器——确定性、可 diff、产物无 eval / 无内联事件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ao.pages import compiler
from ao.pages.schema import PageSchemaError
from tests.unit.test_page_schema import board


def test_compile_is_deterministic() -> None:
    first = compiler.compile_page(board())
    second = compiler.compile_page(json.loads(json.dumps(board())))
    assert first == second
    assert set(first) == {"index.html", "app.js", "theme.css"}


def test_write_page_bytes_are_stable(tmp_path: Path) -> None:
    paths = compiler.write_page(board(), tmp_path / "page")
    snapshot = {name: path.read_bytes() for name, path in paths.items()}

    compiler.write_page(board(), tmp_path / "page2")
    for name, content in snapshot.items():
        assert (tmp_path / "page2" / name).read_bytes() == content


def test_html_escapes_title_and_page_id() -> None:
    raw = board()
    raw["title"] = "日程 & <b>看板</b>"
    files = compiler.compile_page(raw)
    assert "&amp;" in files["index.html"]
    assert "<b>看板</b>" not in files["index.html"]


def test_artifact_has_no_eval_or_inline_handlers() -> None:
    files = compiler.compile_page(board())
    assert "eval(" not in files["app.js"]
    assert "new Function(" not in files["app.js"]
    assert "javascript:" not in files["app.js"]
    assert " onclick=" not in files["index.html"]
    assert " onclick=" not in files["app.js"]
    # 文本节点统一走 textContent，不出现 innerHTML 拼接
    assert "innerHTML" not in files["app.js"]


def test_csp_and_theme_variables() -> None:
    files = compiler.compile_page(board())
    assert "Content-Security-Policy" in files["index.html"]
    assert "default-src 'none'" in files["index.html"]
    assert "--ao-primary: #2f6fed" in files["theme.css"]
    assert "--ao-bg: #f7f8fa" in files["theme.css"]


def test_blocks_are_serialized_into_page_constant() -> None:
    files = compiler.compile_page(board())
    assert '"type": "table"' in files["app.js"]
    assert '"source": "run.list"' in files["app.js"]


def test_schema_with_inline_handler_is_rejected_before_compile() -> None:
    raw = board()
    raw["blocks"][1]["onclick"] = "alert(1)"
    with pytest.raises(PageSchemaError) as exc:
        compiler.compile_page(raw)
    assert "E_EXPR" in {issue.code for issue in exc.value.issues}


def test_unsafe_snippet_in_text_is_rejected() -> None:
    raw = board()
    raw["blocks"].append({"type": "text", "text": "<img src=x onerror=alert(1)>"})
    with pytest.raises(PageSchemaError):
        compiler.compile_page(raw)


def test_assert_safe_guard_is_effective() -> None:
    with pytest.raises(AssertionError):
        compiler._assert_safe("<div/>", "var x = eval('1');")
    with pytest.raises(AssertionError):
        compiler._assert_safe('<div onclick="x()"></div>', "void 0;")
