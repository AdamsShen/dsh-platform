"""MCP 取数。

P1 只提供本地实现与注册表接口：真实 stdio / streamable-http 客户端在 P2 接
（注册表 `bindings` 已经建好，`config` 里放传输方式与端点）。

变量解析失败是常态（MCP 抖动），重试策略在 `ao.vars` 里，不在这里。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class MCPError(RuntimeError):
    """MCP 调用失败：server/tool 不存在、参数缺失、对端报错。"""


_TOOLS: dict[str, Callable[[dict[str, Any]], Any]] = {}
#: 只读工具集合：页面侧 `mcp.read` 只允许调这些，避免"看板按钮顺手改数据"
_READONLY: set[str] = set()

_ROLES = {
    "u_001": {"persona": "安静的插画师，作息偏晚"},
    "u_002": {"persona": "健身教练，早起，日程紧凑"},
    "u_003": {"persona": "夜班护士，白天需要补觉"},
}
_MOOD = {
    "u_001": {"mood": "平静", "energy": 62},
    "u_002": {"mood": "兴奋", "energy": 88},
    "u_003": {"mood": "疲惫", "energy": 31},
}
_BUSY = {
    "u_001": {"busy": ["14:00-15:00 与编辑沟通"]},
    "u_002": {"busy": ["07:00-08:00 早课", "19:00-20:00 私教"]},
    "u_003": {"busy": ["08:00-20:00 值班"]},
}


def _today(_: dict[str, Any]) -> dict[str, Any]:
    from ao.util import utc_now

    return {"date": utc_now().date().isoformat()}


def _role(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("id") or args.get("role_id") or "")
    if key not in _ROLES:
        raise MCPError(f"角色不存在: {key}")
    return dict(_ROLES[key])


def _mood(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("role_id") or args.get("id") or "")
    if key not in _MOOD:
        raise MCPError(f"情绪数据不存在: {key}")
    return dict(_MOOD[key])


def _busy(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("role_id") or args.get("id") or "")
    if key not in _BUSY:
        raise MCPError(f"日历数据不存在: {key}")
    return dict(_BUSY[key])


def register(
    server: str, tool: str, fn: Callable[[dict[str, Any]], Any], *, readonly: bool = False
) -> None:
    """登记一个工具；`readonly=True` 才允许被页面 `mcp.read` 调用。

    默认 False（写操作）是有意的：新增工具忘了标只读时，页面调不动而不是能乱写。
    """
    name = f"{normalize(server)}.{tool}"
    _TOOLS[name] = fn
    if readonly:
        _READONLY.add(name)
    else:
        _READONLY.discard(name)


def is_readonly(server: str, tool: str) -> bool:
    return f"{normalize(server)}.{tool}" in _READONLY


def call_readonly(server: str, tool: str, args: dict[str, Any]) -> Any:
    """只读调用入口：非只读工具直接拒绝，不落到 `call`。"""
    if not is_readonly(server, tool):
        raise MCPError(f"非只读工具不允许从页面调用: {server}.{tool}")
    return call(server, tool, args)





def normalize(server: str) -> str:
    """`global.calendar` → `calendar`；作用域前缀不参与本地工具表。"""
    return server.split(".", 1)[-1]


def call(server: str, tool: str, args: dict[str, Any]) -> Any:
    name = f"{normalize(server)}.{tool}"
    fn = _TOOLS.get(name)
    if fn is None:
        raise MCPError(f"未知 MCP 工具: {server}.{tool}")
    try:
        return fn(dict(args or {}))
    except MCPError:
        raise
    except Exception as exc:  # noqa: BLE001 — 对端异常统一归类
        raise MCPError(f"MCP 调用失败 {server}.{tool}: {exc}") from exc


def describe(server: str, tool: str, args: dict[str, Any]) -> str:
    rendered = ",".join(f"{k}={v}" for k, v in sorted((args or {}).items()))
    return f"mcp__{normalize(server)}__{tool}({rendered})"


# 本地内置工具都是取数语义（今天/角色/情绪/日历），登记为只读；
# 放在模块末尾是因为 register 依赖 normalize 的定义顺序。
for _server, _tool, _fn in (
    ("clock", "today", _today),
    ("roles", "get", _role),
    ("mood", "latest", _mood),
    ("calendar", "busy", _busy),
):
    register(_server, _tool, _fn, readonly=True)
