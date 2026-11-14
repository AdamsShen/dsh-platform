from __future__ import annotations

from ao.manifest import parse
from ao.static_check import check_source

MANIFEST = parse(
    {
        "id": "wf",
        "nodes": [{"id": "draft", "outputs": {"text": "str"}}, {"id": "save", "outputs": {"ok": "bool"}}],
        "edges": [{"from": "draft", "to": "save"}],
    }
)

GOOD = '''
from wf_sdk import agent, graph, node, parse_contract

@node(id="draft", outputs={"text": str})
async def draft(ctx):
    text = await agent()
    return parse_contract(text, ctx.contract)

@node(id="save", outputs={"ok": bool})
async def save(ctx):
    raw = await agent()
    return parse_contract(raw, ctx.contract)

G = graph(nodes=[draft, save], edges=[{"from": "draft", "to": "save"}])
'''


def codes(source: str) -> set[str]:
    return {issue.code for issue in check_source(source, MANIFEST)}


def test_good_source_has_no_issues() -> None:
    assert codes(GOOD) == set()


def test_rejects_extra_import() -> None:
    assert "E_IMPORT" in codes(GOOD.replace("from wf_sdk", "import os\nfrom wf_sdk"))


def test_rejects_eval() -> None:
    assert "E_EVAL" in codes(GOOD.replace("text = await agent()", 'text = eval("1+1")'))


def test_rejects_module_level_side_effect() -> None:
    assert "E_SIDE_EFFECT" in codes(GOOD + "\nopen('/etc/passwd').read()\n")


def test_rejects_unbounded_loop() -> None:
    assert "E_LOOP_UNBOUNDED" in codes(
        GOOD.replace("return parse_contract(text, ctx.contract)", "while True:\n        pass")
    )


def test_rejects_attribute_escape() -> None:
    assert "E_ATTR_ESCAPE" in codes(
        GOOD.replace("return parse_contract(text, ctx.contract)", "return ().__class__")
    )


def test_rejects_graph_mismatch() -> None:
    bad = GOOD.replace('edges=[{"from": "draft", "to": "save"}]', "edges=[]")
    assert "E_GRAPH_MISMATCH" in codes(bad)


def test_rejects_missing_graph() -> None:
    assert "E_GRAPH_MISMATCH" in codes(GOOD.replace("G = graph(", "G0 = graph("))
