"""manifest：工作流的唯一真值。

代码（graph.py）是产物，提示词是独立文件；manifest 只描述节点、边、绑定、变量与上限。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ao.cron import validate_cron

OUTPUT_TYPES = {"str", "int", "float", "bool"}
_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_WHEN_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


class Retry(BaseModel):
    max: int = 0
    backoff: float = 30.0


class Limits(BaseModel):
    max_nodes: int = 20
    max_turns: int = 200
    max_loops: int = 3
    max_wall_seconds: int = 1800


class Node(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: Literal["agent", "action"] = "agent"
    title: str | None = None
    #: 包内相对路径；内容由 wf_sdk 按节点 id 读取，代码里不内联提示词
    system: str | None = None
    prompt: str | None = None
    mcp: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    capability: str = "read_only"
    contract: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    when: str | None = None
    timeout: int = 600
    retry: Retry = Field(default_factory=Retry)


class Edge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    src: str = Field(alias="from")
    dst: str = Field(alias="to")
    when: str | None = None


class Trigger(BaseModel):
    type: str = "manual"
    cron: str | None = None
    timezone: str = "Asia/Shanghai"
    #: 事件型触发要匹配的事件名（如 order.created）
    event_name: str | None = None
    #: cron 型回填：停机后最多补多久内的时刻、一次最多补几个
    catchup_window_seconds: int = 3600
    catchup_limit: int = 1
    #: 重叠策略（P2-T19）：skip 跳过 / buffer_one 缓冲一个 / cancel_other 置旧 run 取消后启动
    overlap_policy: str = "skip"
    fanout: dict[str, Any] | None = None


class Binding(BaseModel):
    """工作流级工具绑定：MCP server 或 skill。

    绑定属于整个工作流而不是节点——"能用哪些工具"是运行环境的一部分，
    节点只描述"做什么"。节点级 `mcp/skills` 字段保留解析（老包兼容），生成器不再写。
    """

    kind: Literal["mcp", "skill"]
    ref: str
    scope: Literal["global", "private"] = "global"
    #: private 绑定时必须是引用者本人；global 为空
    owner: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    manifest_version: int = 2
    id: str
    version: int = 1
    name: str | None = None
    tenant: str = "default"
    parent_id: str | None = None
    limits: Limits = Field(default_factory=Limits)
    vars: dict[str, Any] = Field(default_factory=dict)
    nodes: list[Node]
    edges: list[Edge] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    bindings: list[Binding] = Field(default_factory=list)
    ui: dict[str, Any] = Field(default_factory=dict)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def effective_bindings(self) -> list[Binding]:
        """工作流级绑定 + 老包节点级引用的并集。

        节点级引用没有 scope 信息，按 global 处理：它们本来就是自由字符串引用，
        历史上也没有私有概念。同名去重，工作流级优先。
        """
        merged: list[Binding] = list(self.bindings)
        seen = {(b.kind, b.ref) for b in merged}
        for node in self.nodes:
            for kind, refs in (("mcp", node.mcp), ("skill", node.skills)):
                for ref in refs:
                    key = (kind, ref)
                    if key not in seen:
                        seen.add(key)
                        merged.append(Binding(kind=kind, ref=ref, scope="global"))  # type: ignore[arg-type]
        return merged

    def mcp_refs(self) -> list[str]:
        return [b.ref for b in self.effective_bindings() if b.kind == "mcp"]

    def skill_refs(self) -> list[str]:
        return [b.ref for b in self.effective_bindings() if b.kind == "skill"]


def parse(data: dict[str, Any]) -> Manifest:
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"manifest 结构不合法: {exc.errors()[:3]}") from exc
    validate_graph(manifest)
    return manifest


def validate_graph(manifest: Manifest) -> None:
    ids = [n.id for n in manifest.nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("节点 id 重复")
    if not ids:
        raise ValueError("至少需要一个节点")
    if len(ids) > manifest.limits.max_nodes:
        raise ValueError(f"节点数 {len(ids)} 超过上限 {manifest.limits.max_nodes}")

    known = set(ids)
    for edge in manifest.edges:
        if edge.src not in known or edge.dst not in known:
            raise ValueError(f"边指向不存在的节点: {edge.src} -> {edge.dst}")

    # 触发器：cron/事件配置非法要在这里拦住，否则会一直顺利发布、等到点才失败
    for trigger in manifest.triggers:
        if trigger.type not in {"manual", "cron", "event"}:
            raise ValueError(
                f"不支持的触发器类型: {trigger.type}（只支持 manual/cron/event）"
            )
        if trigger.type == "cron":
            try:
                validate_cron(trigger.cron or "", trigger.timezone)
            except ValueError as exc:
                raise ValueError(f"cron 触发器不合法: {exc}") from exc
            if not 0 <= trigger.catchup_window_seconds <= 30 * 24 * 3600:
                raise ValueError("catchup_window_seconds 必须在 0~2592000 之间")
            if not 1 <= trigger.catchup_limit <= 20:
                raise ValueError("catchup_limit 必须在 1~20 之间")
            # 策略拼错不能静默回落：那会让"以为在缓冲"的计划实际一直在跳过
            if trigger.overlap_policy not in {"skip", "buffer_one", "cancel_other"}:
                raise ValueError(
                    "overlap_policy 只支持 skip/buffer_one/cancel_other:"
                    f" {trigger.overlap_policy!r}"
                )
        if trigger.type == "event" and not (trigger.event_name or "").strip():
            raise ValueError("event 触发器必须声明 event_name")

    seen_bindings: set[tuple[str, str]] = set()
    for binding in manifest.bindings:
        if not _REF_RE.match(binding.ref):
            raise ValueError(
                f"绑定的 ref 不合法: {binding.ref!r}（小写字母/数字开头，允许 _ . -，最长 64）"
            )
        key = (binding.kind, binding.ref)
        if key in seen_bindings:
            raise ValueError(f"重复绑定: {binding.kind}:{binding.ref}")
        seen_bindings.add(key)
        if binding.scope == "private" and not binding.owner:
            raise ValueError(f"私有绑定缺少 owner: {binding.kind}:{binding.ref}")

    for node in manifest.nodes:
        for name, kind in node.outputs.items():
            if kind not in OUTPUT_TYPES:
                raise ValueError(f"节点 {node.id} 的 outputs.{name} 类型不支持: {kind}")
        if node.when:
            match = _WHEN_RE.match(node.when)
            if not match:
                raise ValueError(f"when 表达式不支持: {node.when}")
            ref = match.group(1)
            if ref not in known:
                raise ValueError(f"when 引用了不存在的节点: {ref}")
            if ref == node.id:
                raise ValueError(f"节点 {node.id} 的 when 不能引用自己")

    # 边上的 when 是对**上游节点产物**的条件（`draft.conflict_count > 0`），
    # 天然会引用边的起点，不做"不能引用自己"的检查。
    for edge in manifest.edges:
        if not edge.when:
            continue
        match = _WHEN_RE.match(edge.when)
        if not match:
            raise ValueError(f"when 表达式不支持: {edge.when}")
        if match.group(1) not in known:
            raise ValueError(f"when 引用了不存在的节点: {match.group(1)}")

    topo_order(manifest)  # 有环会抛错


def topo_order(manifest: Manifest) -> list[Node]:
    index = {n.id: n for n in manifest.nodes}
    indegree = {n.id: 0 for n in manifest.nodes}
    children: dict[str, list[str]] = {n.id: [] for n in manifest.nodes}
    for edge in manifest.edges:
        children[edge.src].append(edge.dst)
        indegree[edge.dst] += 1

    ready = [n.id for n in manifest.nodes if indegree[n.id] == 0]
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(manifest.nodes):
        raise ValueError("manifest 的边存在环")
    return [index[i] for i in ordered]


def parse_when(expr: str) -> tuple[str, str, str, Any]:
    match = _WHEN_RE.match(expr or "")
    if not match:
        raise ValueError(f"when 表达式不支持: {expr}")
    node_id, field, op, literal = match.groups()
    text = literal.strip().strip("'\"")
    if re.fullmatch(r"-?\d+", text):
        value: Any = int(text)
    elif re.fullmatch(r"-?\d+\.\d+", text):
        value = float(text)
    elif text.lower() in {"true", "false"}:
        value = text.lower() == "true"
    else:
        value = text
    return node_id, field, op, value


def eval_when(expr: str, outputs: dict[str, dict[str, Any]]) -> bool:
    node_id, field, op, expected = parse_when(expr)
    actual = (outputs.get(node_id) or {}).get(field)
    if actual is None:
        return False
    try:
        return bool(_OPS[op](actual, expected))
    except TypeError:
        return False


def variables_of(manifest: Manifest, node_id: str) -> dict[str, Any]:
    nodes = manifest.vars.get("nodes") or {}
    return dict(nodes.get(node_id) or {})
