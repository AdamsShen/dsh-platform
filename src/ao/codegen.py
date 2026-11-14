"""从 manifest 生成 `graph.py`（骨架 + 声明式补丁）。

生成物是**产物**，不是真值：节点/边/提示词路径都来自 manifest。
同一 manifest 必须生成字节一致的代码（幂等），否则版本 diff 会失去意义。
"""

from __future__ import annotations

import keyword
import re

from ao.manifest import Manifest, Node

SKELETON_VERSION = 1
_TYPE_NAMES = {"str": "str", "int": "int", "float": "float", "bool": "bool"}

_HEADER = '''"""自动生成，请勿手工修改。

skeleton={skeleton}
workflow={workflow_id} version={workflow_version}
"""

from wf_sdk import {imports}

'''


def py_name(node_id: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z_]", "_", node_id)
    if not name or name[0].isdigit():
        name = f"n_{name}"
    if keyword.iskeyword(name):
        name = f"{name}_node"
    return name


def _outputs_literal(node: Node) -> str:
    if not node.outputs:
        return ""
    pairs = ", ".join(
        f'"{name}": {_TYPE_NAMES.get(kind, "str")}' for name, kind in sorted(node.outputs.items())
    )
    return f", outputs={{{pairs}}}"


def _node_block(node: Node) -> str:
    func = py_name(node.id)
    title = f', title="{node.title}"' if node.title else ""
    kind = ', kind="action"' if node.kind == "action" else ""
    if node.kind == "action":
        body = f'    return await action("{node.id}")'
    else:
        body = "    text = await agent()\n    return parse_contract(text, ctx.contract)"
    return (
        f'@node(id="{node.id}"{_outputs_literal(node)}{kind}{title})\n'
        f"async def {func}(ctx):\n"
        f"{body}\n\n"
    )


def generate_graph(manifest: Manifest) -> str:
    uses_action = any(node.kind == "action" for node in manifest.nodes)
    uses_agent = any(node.kind != "action" for node in manifest.nodes)
    imports = ["graph", "node"]
    if uses_agent:
        imports += ["agent", "parse_contract"]
    if uses_action:
        imports.append("action")

    parts = [
        _HEADER.format(
            skeleton=SKELETON_VERSION,
            workflow_id=manifest.id,
            workflow_version=manifest.version,
            imports=", ".join(sorted(set(imports), key=imports.index)),
        )
    ]
    for node in manifest.nodes:
        parts.append(_node_block(node))

    names = ", ".join(py_name(node.id) for node in manifest.nodes)
    edges = []
    for edge in manifest.edges:
        item = f'{{"from": "{edge.src}", "to": "{edge.dst}"'
        if edge.when:
            item += f', "when": "{edge.when}"'
        edges.append(item + "}")
    edge_text = "[" + ", ".join(edges) + "]"
    entry = f', entry="{py_name(manifest.nodes[0].id)}"' if len(manifest.nodes) > 1 else ""
    parts.append(f"G = graph(nodes=[{names}], edges={edge_text}{entry})\n")
    return "".join(parts)
