"""变量解析：Jinja2 渲染 + MCP 取数 + run 级缓存。

规则（设计 §8.2）：
1. run 级变量在 run 开始时一次解析；
2. 节点级变量在节点进入时懒解析；
3. 值落 `run_vars`，同一节点重跑复用，不重复请求 MCP；
4. 失败重试 1 次（退避 2s），仍失败 → `missing_var`。

agent 兜底需要产品确认（设计 §19.1），默认关闭；打开时显式报错而不是静默降级。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from ao import mcp

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

#: 兜底开关：未确认前不允许打开（避免产出不可复现的值却不留痕）
ALLOW_AGENT_FALLBACK = False


class VarError(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        var: str | None = None,
        tool: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.var = var
        self.tool = tool


def render(template: str, ctx: dict[str, Any]) -> str:
    try:
        return _env.from_string(template or "").render(**ctx)
    except Exception as exc:  # noqa: BLE001 — 渲染失败统一归为模板错误
        raise VarError("template_error", f"模板渲染失败: {exc}") from exc


def render_value(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return render(value, ctx)
    if isinstance(value, dict):
        return {k: render_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, ctx) for v in value]
    return value


def resolve_specs(
    specs: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
    *,
    cached: dict[str, Any] | None = None,
    persist: Callable[[str, Any, str], None] | None = None,
    retries: int = 1,
    backoff: float = 2.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """解析一组变量声明，返回 (值, 调用记录)。"""
    cached = cached or {}
    values: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    for name, spec in specs.items():
        if name in cached:
            values[name] = cached[name]
            calls.append({"var": name, "ok": True, "cached": True})
            continue
        server = str(spec.get("mcp") or "")
        tool = str(spec.get("tool") or "")
        if not server or not tool:
            raise VarError("declaration_error", f"变量 {name} 缺少 mcp/tool 声明")
        args = render_value(spec.get("args") or {}, ctx)
        attempt = 0
        while True:
            try:
                value = mcp.call(server, tool, args)
                values[name] = value
                if persist is not None:
                    persist(name, value, "mcp")
                calls.append(
                    {
                        "var": name,
                        "tool": mcp.describe(server, tool, args),
                        "ok": True,
                        "attempt": attempt,
                    }
                )
                break
            except Exception as exc:  # noqa: BLE001 — MCP 抖动是常态
                attempt += 1
                if attempt > retries:
                    if ALLOW_AGENT_FALLBACK:  # pragma: no cover - 产品未确认
                        raise VarError(
                            "fallback_requested",
                            f"变量 {name} 取数失败，agent 兜底尚未启用（设计 §19.1 待确认）",
                        ) from exc
                    calls.append(
                        {
                            "var": name,
                            "tool": mcp.describe(server, tool, args),
                            "ok": False,
                            "attempt": attempt,
                            "error": str(exc),
                        }
                    )
                    raise VarError(
                        "missing_var",
                        f"变量 {name} 取数失败（{server}.{tool}）: {exc}",
                        var=name,
                        tool=mcp.describe(server, tool, args),
                    ) from exc
                time.sleep(backoff)
    return values, calls
