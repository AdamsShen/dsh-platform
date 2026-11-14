"""wf_sdk：生成代码唯一可用的平台接口。

生成代码里只允许出现：

    from wf_sdk import graph, node, agent, action, emit, parse_contract

不允许 `os` / `subprocess` / `socket` / `importlib` / `ctypes`；
静态检查与运行期沙箱都由平台侧负责，本模块只保证"正常路径不需要别的 import"。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from wf_sdk.runtime import (
    TYPE_MAP,
    ContractError,
    GraphSpec,
    NodeContext,
    NodeSpec,
    coerce_outputs,
    current_context,
    current_run_env,
    load_contract_file,
    reset_context,
    set_context,
)

__all__ = [
    "graph",
    "node",
    "agent",
    "action",
    "emit",
    "parse_contract",
    "register_action",
    "ContractError",
    "NodeContext",
    "GraphSpec",
    "NodeSpec",
]

_RESULT_RE = re.compile(r"```result\s*\n(.*?)\n```", re.S)

#: action 节点可调用的平台动作；P2 会改成 manifest 显式声明 + 静态检查。
_ACTIONS: dict[str, Callable[..., Any]] = {}


def register_action(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _ACTIONS:
            raise ValueError(f"action 重复注册: {name}")
        _ACTIONS[name] = fn
        return fn

    return decorator


def node(
    id: str,  # noqa: A002 — 与生成代码的写法保持一致
    outputs: dict[str, type] | None = None,
    *,
    kind: str = "agent",
    title: str | None = None,
    timeout: float | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """声明一个节点。

    被装饰的函数签名固定为 `async def fn(ctx: NodeContext) -> dict`，
    返回值必须满足 `outputs` 声明，否则节点失败（走 repair 一次）。
    """
    types = dict(outputs or {})
    for name, kind_value in types.items():
        if isinstance(kind_value, str):
            if kind_value not in TYPE_MAP:
                raise ValueError(f"节点 {id} 的 outputs.{name} 类型不支持: {kind_value}")
            types[name] = TYPE_MAP[kind_value]
        elif not isinstance(kind_value, type):
            raise ValueError(f"节点 {id} 的 outputs.{name} 必须是类型或类型名")

    spec = NodeSpec(id=id, outputs=types, kind=kind, title=title, timeout=timeout)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        async def runner(state: dict[str, Any]) -> dict[str, Any]:
            env = current_run_env()
            ctx = NodeContext(
                run_id=str(state.get("run_id") or ""),
                node_id=id,
                attempt=env.attempt,
                vars=dict(state.get("vars") or {}),
                nodes=dict(state.get("nodes") or {}),
                subject=dict(state.get("subject") or {}),
                package_root=env.package_root,
                workspace=env.workspace,
                artifacts=env.artifacts,
                inputs=env.inputs,
                contract=load_contract_file(env.package_root, id),
                mcp=list(env.mcp),
                skills=list(env.skills),
                runtime=env.runtime,
            )
            token = set_context(ctx)
            try:
                if ctx.runtime is not None:
                    ctx.runtime.node_started(ctx, spec)
                try:
                    result = await fn(ctx)
                    if not isinstance(result, dict):
                        raise ContractError(
                            [f"节点 {id} 必须返回 dict，实际是 {type(result).__name__}"]
                        )
                    data, errors = coerce_outputs(result, spec.outputs)
                    if errors:
                        raise ContractError(errors)
                except ContractError as exc:
                    # 契约不合格：同一会话内 repair 一次；repair 也失败则节点失败。
                    if ctx.runtime is None:
                        raise
                    data = await ctx.runtime.repair(ctx, spec, exc.errors)
                if ctx.runtime is not None:
                    ctx.runtime.node_finished(ctx, spec, data)
                nodes = dict(ctx.nodes)
                nodes[id] = data
                return {"nodes": nodes, "last": id}
            except Exception as exc:
                if ctx.runtime is not None:
                    ctx.runtime.node_failed(ctx, spec, exc)
                raise
            finally:
                reset_context(token)

        runner.__wf_node__ = spec  # type: ignore[attr-defined]
        runner.__name__ = getattr(fn, "__name__", id)
        runner.__doc__ = getattr(fn, "__doc__", None)
        return runner

    return decorator


def graph(
    nodes: Iterable[Callable[..., Any]],
    edges: Iterable[dict[str, str]],
    *,
    entry: str | Iterable[str] | None = None,
) -> GraphSpec:
    """把节点函数与边声明组装成图描述；渲染由执行器完成。"""
    specs: list[tuple[NodeSpec, Callable[..., Any]]] = []
    for fn in nodes:
        spec = getattr(fn, "__wf_node__", None)
        if spec is None:
            raise ValueError(f"{getattr(fn, '__name__', fn)} 不是 @node 装饰过的函数")
        specs.append((spec, fn))
    entry_list: list[str] | None
    if entry is None:
        entry_list = None
    elif isinstance(entry, str):
        entry_list = [entry]
    else:
        entry_list = list(entry)
    return GraphSpec(nodes=specs, edges=[dict(e) for e in edges], entry=entry_list)


async def agent(
    prompt: str | None = None,
    *,
    vars: dict[str, Any] | None = None,  # noqa: A002 — 与生成代码保持一致的参数名
    mcp: list[str] | None = None,
    skills: list[str] | None = None,
) -> str:
    """一次 harness turn。返回原始文本，契约解析交给 `parse_contract`。"""
    ctx = current_context()
    if ctx.runtime is None:
        raise RuntimeError("没有执行器，agent() 不可用")
    text = prompt if prompt is not None else ctx.prompt()
    return await ctx.runtime.call_agent(
        ctx,
        prompt=text,
        vars=dict(vars if vars is not None else ctx.vars),
        mcp=list(mcp if mcp is not None else ctx.mcp),
        skills=list(skills if skills is not None else ctx.skills),
    )


async def action(name: str, **params: Any) -> Any:
    """调用平台白名单动作；只有 `kind="action"` 的节点允许调用。"""
    ctx = current_context()
    if ctx.runtime is not None:
        ctx.runtime.assert_action_allowed(ctx)
    fn = _ACTIONS.get(name)
    if fn is None:
        raise KeyError(f"未注册的 action: {name}")
    if ctx.runtime is not None and hasattr(ctx.runtime, "action_once"):
        # 执行器负责做去重（同一 run/node/action/参数只真执行一次）
        return await ctx.runtime.action_once(ctx, name, dict(params), fn)
    result = fn(ctx=ctx, **params)
    if hasattr(result, "__await__"):
        return await result
    return result


def emit(event: str, **payload: Any) -> None:
    """落一条事件（审计与详情页都读它）。"""
    current_context().emit(event, **payload)


def parse_contract(text: str, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """取最后一个 ```result 块，按 contract 校验字段。"""
    matches = _RESULT_RE.findall(text or "")
    if not matches:
        raise ContractError(["没有找到合法的 ```result 块"])
    raw = matches[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError([f"result 块不是合法 JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ContractError(["result 块必须是 JSON 对象"])
    if not contract:
        return data

    required = contract.get("required") or {}
    optional = contract.get("optional") or {}
    merged = {**required, **optional}
    types: dict[str, type] = {}
    for name, kind in merged.items():
        if isinstance(kind, str) and kind in TYPE_MAP:
            types[name] = TYPE_MAP[kind]
    if not types:
        return data
    result, errors = coerce_outputs(
        {k: v for k, v in data.items() if k in types},
        {k: v for k, v in types.items() if k in required} or types,
    )
    if errors:
        raise ContractError(errors)
    return {**{k: v for k, v in data.items() if k not in types}, **result}


def __getattr__(name: str) -> Any:
    # 便于执行器在测试里取内部对象，不鼓励生成代码使用
    if name in {"NodeContext", "GraphSpec", "NodeSpec"}:
        return globals()[name]
    raise AttributeError(name)
