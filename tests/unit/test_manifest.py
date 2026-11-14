from __future__ import annotations

import pytest

from ao.manifest import parse, topo_order


def base(**overrides):
    data = {
        "manifest_version": 2,
        "id": "wf",
        "version": 1,
        "nodes": [
            {"id": "a", "outputs": {"x": "int"}},
            {"id": "b", "when": "a.x > 0"},
        ],
        "edges": [{"from": "a", "to": "b", "when": "a.x > 0"}],
    }
    data.update(overrides)
    return data


def test_parse_ok() -> None:
    manifest = parse(base())
    assert [n.id for n in topo_order(manifest)] == ["a", "b"]


def test_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="节点 id 重复"):
        parse(base(nodes=[{"id": "a"}, {"id": "a"}]))


def test_edge_to_missing_node() -> None:
    with pytest.raises(ValueError, match="边指向不存在的节点"):
        parse(base(edges=[{"from": "a", "to": "nope"}]))


def test_cycle() -> None:
    with pytest.raises(ValueError, match="存在环"):
        parse(
            base(
                nodes=[{"id": "a"}, {"id": "b"}],
                edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
            )
        )


def test_when_references_unknown_node() -> None:
    with pytest.raises(ValueError, match="when 引用了不存在的节点"):
        parse(base(nodes=[{"id": "a"}, {"id": "b", "when": "ghost.x > 0"}], edges=[]))


def test_unsupported_output_type() -> None:
    with pytest.raises(ValueError, match="类型不支持"):
        parse(base(nodes=[{"id": "a", "outputs": {"x": "list"}}], edges=[]))


def test_max_nodes_limit() -> None:
    nodes = [{"id": f"n{i}"} for i in range(5)]
    with pytest.raises(ValueError, match="超过上限"):
        parse(base(nodes=nodes, edges=[], limits={"max_nodes": 3}))
