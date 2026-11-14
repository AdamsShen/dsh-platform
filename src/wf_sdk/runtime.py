"""wf_sdk 的运行时对象：节点声明、图声明、节点上下文。

生成的 `graph.py` 只能依赖本模块暴露的能力，平台侧的 provider、数据库、
文件系统细节都不在这里出现，避免生成代码拿到任意能力。
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

#: 当前节点的上下文，由执行器在进入节点时设置；生成代码不直接读写它。
_current_ctx: contextvars.ContextVar[NodeContext | None] = contextvars.ContextVar(
    "wf_sdk_current_ctx", default=None
)


@dataclass
class RunEnv:
    """一个 run 的执行环境。

    不放进 LangGraph state：Path 与 provider 不可 JSON 序列化，checkpoint 会写不进去。
    """

    package_root: Path
    workspace: Path
    artifacts: Path
    inputs: Path | None
    runtime: Any
    attempt: int = 0
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


_run_env: contextvars.ContextVar[RunEnv | None] = contextvars.ContextVar(
    "wf_sdk_run_env", default=None
)


def set_run_env(env: RunEnv) -> contextvars.Token:
    return _run_env.set(env)


def reset_run_env(token: contextvars.Token) -> None:
    _run_env.reset(token)


def current_run_env() -> RunEnv:
    env = _run_env.get()
    if env is None:
        raise RuntimeError("节点执行环境未初始化")
    return env


class ContractError(RuntimeError):
    """产物不满足节点契约。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors) or "产物不合格")
        self.errors = errors


TYPE_MAP: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}


@dataclass
class NodeSpec:
    id: str
    outputs: dict[str, type] = field(default_factory=dict)
    kind: str = "agent"
    title: str | None = None
    timeout: float | None = None

    def as_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "outputs": {name: t.__name__ for name, t in self.outputs.items()},
            "timeout": self.timeout,
        }


@dataclass
class GraphSpec:
    nodes: list[tuple[NodeSpec, Callable]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    entry: list[str] | None = None

    def node_ids(self) -> list[str]:
        return [spec.id for spec, _ in self.nodes]

    def edge_pairs(self) -> list[tuple[str, str]]:
        return [(edge["from"], edge["to"]) for edge in self.edges]


@dataclass
class NodeContext:
    run_id: str
    node_id: str
    attempt: int
    vars: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    subject: dict[str, Any]
    package_root: Path
    workspace: Path
    artifacts: Path
    inputs: Path | None = None
    contract: dict[str, Any] | None = None
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    runtime: Any = None

    # --- 模板 ---
    def render(self, template: str) -> str:
        try:
            return _env.from_string(template or "").render(
                subject=self.subject,
                vars=self.vars,
                nodes=self.nodes,
                run_id=self.run_id,
            )
        except Exception as exc:  # noqa: BLE001 — 统一抛给执行器归类
            raise ValueError(f"模板渲染失败: {exc}") from exc

    def _read_prompt(self, name: str) -> str:
        path = self.package_root / "prompts" / name
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def prompt(self) -> str:
        return self.render(self._read_prompt(f"{self.node_id}.md"))

    def system(self) -> str:
        return self.render(self._read_prompt(f"{self.node_id}.system.md"))

    # --- 文件（只有 workspace / artifacts 可写） ---
    def read_input(self, name: str) -> str:
        if self.inputs is None:
            raise PermissionError("该节点没有 input/ 目录")
        target = (self.inputs / name).resolve()
        if self.inputs.resolve() not in target.parents and target != self.inputs.resolve():
            raise PermissionError(f"越界读取: {name}")
        return target.read_text(encoding="utf-8")

    def write_artifact(self, name: str, content: str | bytes) -> str:
        target = (self.artifacts / name).resolve()
        if self.artifacts.resolve() not in target.parents and target != self.artifacts.resolve():
            raise PermissionError(f"越界写入: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.package_root))

    def emit(self, event: str, **payload: Any) -> None:
        if self.runtime is not None:
            self.runtime.emit(self, event, payload)


def set_context(ctx: NodeContext) -> contextvars.Token:
    return _current_ctx.set(ctx)


def reset_context(token: contextvars.Token) -> None:
    _current_ctx.reset(token)


def current_context() -> NodeContext:
    ctx = _current_ctx.get()
    if ctx is None:
        raise RuntimeError("agent()/emit() 只能在节点执行期间调用")
    return ctx


def coerce_outputs(
    data: dict[str, Any], outputs: dict[str, type]
) -> tuple[dict[str, Any], list[str]]:
    """按声明类型转换；未声明字段原样保留。"""
    result = dict(data)
    errors: list[str] = []
    for name, kind in outputs.items():
        if name not in data:
            errors.append(f"缺少字段 {name}")
            continue
        raw = data[name]
        try:
            if kind is bool:
                text = str(raw).strip().lower()
                if text in {"true", "1", "yes", "y"}:
                    result[name] = True
                elif text in {"false", "0", "no", "n", ""}:
                    result[name] = False
                else:
                    raise ValueError(f"无法转成 bool: {raw!r}")
            else:
                result[name] = kind(raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"字段 {name} 类型应为 {kind.__name__}: {exc}")
    return result, errors


def load_contract_file(package_root: Path, node_id: str) -> dict[str, Any] | None:
    path = package_root / "contracts" / f"{node_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
