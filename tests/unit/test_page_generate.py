"""W47：生成期页面——Schema 可校验、产物可冒烟、缺渲染器能被点名。"""

from __future__ import annotations

import json
from pathlib import Path

from ao.pages import compiler
from ao.pages import schema as page_schema
from ao.pages.generate import generate_page, smoke_page


def test_generated_page_passes_schema_validation() -> None:
    page = generate_page(workflow_id="wf_gen", title="小林 的工作流")
    clean = page_schema.load(page)
    assert clean["title"] == "小林 的工作流"
    types = [block["type"] for block in clean["blocks"]]
    # 验收要求：对话产出的页面带表格 + 详情
    assert "table" in types and "detail" in types
    assert {"runs", "run", "events"} <= set(clean["data"])


def test_generated_page_compiles_to_all_artifacts(tmp_path: Path) -> None:
    page = page_schema.load(generate_page(workflow_id="wf_gen", title="看板"))
    root = tmp_path / "pkg"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "ui" / "pages" / "main.json").write_text(
        json.dumps(page, ensure_ascii=False), encoding="utf-8"
    )
    compiler.write_page(page, root / "ui" / "compiled" / "main")

    assert smoke_page(root, "main") == []
    # 冒烟不是"文件存在就算过"：产物必须自描述（data-page 与数据源名）
    js = (root / "ui" / "compiled" / "main" / "app.js").read_text(encoding="utf-8")
    assert "run.list" in json.dumps(page, ensure_ascii=False)
    assert "data-page" in (root / "ui" / "compiled" / "main" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "fetchSource" in js


def test_smoke_reports_missing_component_name(tmp_path: Path) -> None:
    page = page_schema.load(generate_page(workflow_id="wf_gen", title="看板"))
    page["blocks"].append(
        {"type": "chart", "kind": "bar", "title": "趋势", "source": "runs",
         "xField": "status", "yField": "spent_usd"}
    )
    root = tmp_path / "pkg"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "ui" / "pages" / "main.json").write_text(
        json.dumps(page, ensure_ascii=False), encoding="utf-8"
    )
    compiler.write_page(page, root / "ui" / "compiled" / "main")

    issues = smoke_page(root, "main")
    assert len(issues) == 1
    assert issues[0]["code"] == "E_PAGE_SMOKE"
    assert issues[0]["components"] == ["chart"]
    assert "chart" in issues[0]["message"]


def test_smoke_reports_missing_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    (root / "ui" / "pages").mkdir(parents=True)
    issues = smoke_page(root, "main")
    assert {issue["message"] for issue in issues} == {
        "编译产物缺失: index.html",
        "编译产物缺失: app.js",
        "编译产物缺失: theme.css",
    }
