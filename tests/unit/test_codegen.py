from __future__ import annotations

import ast

from ao.codegen import SKELETON_VERSION, generate_graph, py_name
from ao.manifest import parse
from ao.package import load_package
from ao.settings import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"


def example_manifest():
    return parse(load_package(EXAMPLE, verify=False).manifest)


def test_generate_is_idempotent() -> None:
    manifest = example_manifest()
    first = generate_graph(manifest)
    second = generate_graph(manifest)
    assert first == second
    assert f"skeleton={SKELETON_VERSION}" in first
    ast.parse(first)


def test_generated_code_matches_manifest() -> None:
    from ao.static_check import check_source

    manifest = example_manifest()
    source = generate_graph(manifest)
    assert check_source(source, manifest) == []
    assert "await agent()" in source
    assert "parse_contract(text, ctx.contract)" in source
    assert 'entry="draft"' in source


def test_py_name_sanitizes() -> None:
    assert py_name("draft-schedule") == "draft_schedule"
    assert py_name("1st") == "n_1st"
    assert py_name("class") == "class_node"


def test_action_node_imports_action() -> None:
    manifest = parse(
        {
            "id": "wf",
            "nodes": [{"id": "draft"}, {"id": "save", "kind": "action", "outputs": {"ok": "bool"}}],
            "edges": [{"from": "draft", "to": "save"}],
        }
    )
    source = generate_graph(manifest)
    import_line = next(line for line in source.splitlines() if line.startswith("from wf_sdk"))
    assert "action" in import_line
    assert 'return await action("save")' in source
