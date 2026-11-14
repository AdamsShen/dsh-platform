"""生成代码的静态检查（AST 白名单）。

不是沙箱：沙箱在运行期，本模块只负责"把明显越界的东西挡在发布之前"。
误报优先补规则，不放宽成黑名单。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from ao.manifest import Manifest

ALLOWED_IMPORTS = {"wf_sdk", "typing", "json", "datetime", "re"}
BANNED_ATTRS = {"__class__", "__bases__", "__subclasses__", "__globals__", "__code__", "__mro__"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}


@dataclass
class Issue:
    code: str
    message: str
    line: int

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "line": self.line}


def _module_level_ok(statement: ast.stmt) -> bool:
    if isinstance(
        statement,
        (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.AnnAssign),
    ):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return isinstance(statement.value.value, str)  # 模块 docstring
    if isinstance(statement, ast.Assign):
        value = statement.value
        if isinstance(value, (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Name)):
            return True
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return value.func.id == "graph"  # G = graph(...)
        return False
    if isinstance(statement, ast.If):
        test = statement.test
        return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
    return False


def _node_ids_from_ast(tree: ast.Module) -> dict[str, str]:
    """函数名 → 节点 id。"""
    mapping: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in statement.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Name) or decorator.func.id != "node":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    mapping[statement.name] = str(keyword.value.value)
    return mapping


def _graph_decl(tree: ast.Module) -> tuple[list[str], list[tuple[str, str]]] | None:
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "G" for t in statement.targets):
            continue
        call = statement.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            return None
        if call.func.id != "graph":
            return None
        names: list[str] = []
        edges: list[tuple[str, str]] = []
        for keyword in call.keywords:
            if keyword.arg == "nodes" and isinstance(keyword.value, ast.List):
                names = [
                    elt.id for elt in keyword.value.elts if isinstance(elt, ast.Name)
                ]
            if keyword.arg == "edges" and isinstance(keyword.value, ast.List):
                for elt in keyword.value.elts:
                    if not isinstance(elt, ast.Dict):
                        continue
                    data = {}
                    for key, value in zip(elt.keys, elt.values, strict=False):
                        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                            data[str(key.value)] = str(value.value)
                    if "from" in data and "to" in data:
                        edges.append((data["from"], data["to"]))
        return names, edges
    return None


def check_source(source: str, manifest: Manifest) -> list[Issue]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Issue("E_SYNTAX", f"语法错误: {exc.msg}", exc.lineno or 0)]

    issues: list[Issue] = []

    for statement in tree.body:
        if not _module_level_ok(statement):
            issues.append(
                Issue(
                    "E_SIDE_EFFECT",
                    "模块级不允许导入以外的副作用",
                    getattr(statement, "lineno", 0),
                )
            )

    func_to_id = _node_ids_from_ast(tree)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if module.split(".")[0] not in ALLOWED_IMPORTS:
                    issues.append(
                        Issue("E_IMPORT", f"不允许 import {module}", getattr(node, "lineno", 0))
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                issues.append(
                    Issue("E_EVAL", f"禁止调用 {node.func.id}", getattr(node, "lineno", 0))
                )
        if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            issues.append(
                Issue("E_ATTR_ESCAPE", f"禁止访问 {node.attr}", getattr(node, "lineno", 0))
            )
        if isinstance(node, ast.While):
            test = node.test
            if isinstance(test, ast.Constant) and test.value in {True}:
                issues.append(
                    Issue("E_LOOP_UNBOUNDED", "禁止 while True", getattr(node, "lineno", 0))
                )
    declared = _graph_decl(tree)
    if declared is None:
        issues.append(Issue("E_GRAPH_MISMATCH", "缺少 G = graph(nodes=[...], edges=[...])", 0))
    else:
        names, edges = declared
        code_ids = {func_to_id.get(name) for name in names}
        manifest_ids = {node.id for node in manifest.nodes}
        if code_ids != manifest_ids:
            issues.append(
                Issue(
                    "E_GRAPH_MISMATCH",
                    f"节点集合不一致: 代码 {sorted(i for i in code_ids if i)}，"
                    f"manifest {sorted(manifest_ids)}",
                    0,
                )
            )
        expected = {(edge.src, edge.dst) for edge in manifest.edges}
        if set(edges) != expected:
            issues.append(
                Issue(
                    "E_GRAPH_MISMATCH",
                    f"边集合不一致: 代码 {sorted(set(edges))}，manifest {sorted(expected)}",
                    0,
                )
            )
    return issues


def check_file(path: Any, manifest: Manifest) -> list[Issue]:
    from pathlib import Path

    return check_source(Path(path).read_text(encoding="utf-8"), manifest)
