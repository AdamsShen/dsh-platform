from __future__ import annotations

from pathlib import Path

import pytest

from wf_sdk import ContractError, action, graph, node, parse_contract, register_action
from wf_sdk.runtime import NodeContext, reset_context, set_context


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


async def test_action_goes_through_runtime_for_dedup() -> None:
    """action() 必须交给执行器做去重，不能在 SDK 里直接调用副作用函数。"""
    calls: list[str] = []

    @register_action("unit_notify")
    def _notify(*, ctx, text: str) -> dict[str, bool]:
        calls.append(text)
        return {"ok": True}

    class FakeRuntime:
        def __init__(self) -> None:
            self.seen: list[object] = []

        def assert_action_allowed(self, ctx: NodeContext) -> None:
            self.seen.append("allowed")

        async def action_once(self, ctx, name, params, fn):  # type: ignore[no-untyped-def]
            self.seen.append((name, params))
            return fn(ctx=ctx, **params)

    runtime = FakeRuntime()
    ctx = NodeContext(
        run_id="r1",
        node_id="n1",
        attempt=0,
        vars={},
        nodes={},
        subject={},
        package_root=Path("."),
        workspace=Path("."),
        artifacts=Path("."),
        runtime=runtime,
    )
    token = set_context(ctx)
    try:
        result = await action("unit_notify", text="hi")
    finally:
        reset_context(token)

    assert result == {"ok": True}
    assert calls == ["hi"]
    assert runtime.seen == ["allowed", ("unit_notify", {"text": "hi"})]
