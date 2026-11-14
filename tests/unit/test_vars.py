from __future__ import annotations

import pytest

from ao import mcp
from ao.vars import VarError, render, resolve_specs


def test_render_missing_variable_is_strict() -> None:
    with pytest.raises(VarError, match="模板渲染失败"):
        render("{{ nope.value }}", {})


def test_render_blocks_attribute_escape() -> None:
    with pytest.raises(VarError):
        render("{{ ''.__class__.__mro__ }}", {})


def test_resolve_uses_cache() -> None:
    specs = {"today": {"mcp": "clock", "tool": "today", "args": {}}}
    values, calls = resolve_specs(specs, {}, cached={"today": {"date": "x"}}, backoff=0)
    assert values["today"] == {"date": "x"}
    assert calls[0]["cached"] is True


def test_unknown_tool_becomes_missing_var() -> None:
    specs = {"x": {"mcp": "ghost", "tool": "nope", "args": {}}}
    with pytest.raises(VarError) as info:
        resolve_specs(specs, {}, backoff=0)
    assert info.value.kind == "missing_var"


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}
    real = mcp.call

    def flaky(server, tool, args):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise mcp.MCPError("抖动")
        return real(server, tool, args)

    monkeypatch.setattr(mcp, "call", flaky)
    values, calls = resolve_specs(
        {"today": {"mcp": "clock", "tool": "today", "args": {}}}, {}, backoff=0
    )
    assert "date" in values["today"]
    assert calls[0]["attempt"] == 1


def test_args_are_rendered_before_call() -> None:
    specs = {
        "role": {"mcp": "roles", "tool": "get", "args": {"id": "{{ subject.user_id }}"}}
    }
    values, _ = resolve_specs(specs, {"subject": {"user_id": "u_001"}}, backoff=0)
    assert "插画师" in values["role"]["persona"]
