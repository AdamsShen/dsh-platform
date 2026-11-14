from __future__ import annotations

import pytest

from wf_sdk import ContractError, graph, node, parse_contract


def test_node_requires_supported_outputs() -> None:
    with pytest.raises(ValueError, match="类型不支持"):
        node(id="a", outputs={"x": "list"})


def test_graph_requires_decorated_nodes() -> None:
    async def raw(ctx):
        return {}

    with pytest.raises(ValueError, match="不是 @node"):
        graph(nodes=[raw], edges=[])


def test_parse_contract_takes_last_block() -> None:
    text = '```result\n{"a": 1}\n```\n中间\n```result\n{"a": 2}\n```'
    assert parse_contract(text) == {"a": 2}


def test_parse_contract_missing_block() -> None:
    with pytest.raises(ContractError, match="没有找到"):
        parse_contract("没有结果")


def test_parse_contract_type_check() -> None:
    contract = {"required": {"count": "int"}}
    assert parse_contract('```result\n{"count": "3"}\n```', contract)["count"] == 3
    with pytest.raises(ContractError, match="类型应为 int"):
        parse_contract('```result\n{"count": "abc"}\n```', contract)
