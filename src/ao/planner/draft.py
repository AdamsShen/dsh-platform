"""创作草稿：生成与发布之间的可编辑中间态。

为什么单独落一份 `draft.json` 而不是直接让人改 `manifest.json`：
- manifest 是给执行器的契约，字段读写有严格校验；草稿要承载"人还没确认的编辑"，
  包括节点标题、职责、提示词正文这些不属于 manifest 的东西；
- 编辑要能反复提交，重建包的过程必须幂等。

草稿只活在 `settings.data_dir/drafts/<session_id>/` 下，随会话生命周期存在。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ao.cron import validate_cron
from ao.settings import settings

#: 允许人在草稿里改的字段；id/kind 不可改（改了边就全废，且不利于回溯）
EDITABLE_FIELDS = ("title", "duty", "outputs", "prompt")
OUTPUT_TYPES = {"str", "int", "bool", "float"}


def draft_dir(session_id: str) -> Path:
    return settings.data_dir / "drafts" / session_id


def draft_path(session_id: str) -> Path:
    return draft_dir(session_id) / "draft.json"


@dataclass
class Draft:
    session_id: str
    workflow_id: str
    name: str
    tenant: str
    requirement: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    #: 工作流级绑定（MCP / Skill），不是节点级
    bindings: list[dict[str, Any]] = field(default_factory=list)
    #: 触发器（目前只有 cron）；空表示只手动触发
    triggers: list[dict[str, Any]] = field(default_factory=list)
    #: 生成时的尝试轮数，发布阶段要沿用（artifact 上要能对上）
    attempts: int = 1

    def save(self) -> None:
        path = draft_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, session_id: str) -> Draft:
        path = draft_path(session_id)
        if not path.exists():
            raise LookupError(f"会话没有草稿: {session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            session_id=data["session_id"],
            workflow_id=data["workflow_id"],
            name=data["name"],
            tenant=data["tenant"],
            requirement=data["requirement"],
            nodes=list(data.get("nodes") or []),
            edges=list(data.get("edges") or []),
            bindings=list(data.get("bindings") or []),
            triggers=list(data.get("triggers") or []),
            attempts=int(data.get("attempts") or 1),
        )


def exists(session_id: str) -> bool:
    return draft_path(session_id).exists()


def apply_edits(draft: Draft, edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把前端提交的节点编辑合并进草稿；返回问题列表（空表示全部合法）。

    `edits` 允许只提交被改动的节点：按 id 定位，其余节点原样保留。
    """
    by_id = {node["id"]: node for node in draft.nodes}
    issues: list[dict[str, Any]] = []
    for edit in edits or []:
        node_id = str(edit.get("id") or "")
        node = by_id.get(node_id)
        if node is None:
            issues.append({"code": "E_UNKNOWN_NODE", "message": f"节点不存在: {node_id}"})
            continue
        title = edit.get("title")
        if title is not None:
            if not str(title).strip():
                issues.append({"code": "E_TITLE", "message": f"{node_id} 的标题不能为空"})
            else:
                node["title"] = str(title).strip()
        duty = edit.get("duty")
        if duty is not None:
            node["duty"] = str(duty).strip()
        prompt = edit.get("prompt")
        if prompt is not None:
            node["prompt"] = str(prompt)
        outputs = edit.get("outputs")
        if outputs is not None:
            if not isinstance(outputs, dict):
                issues.append({"code": "E_OUTPUTS", "message": f"{node_id} 的 outputs 必须是对象"})
                continue
            bad = {k: v for k, v in outputs.items() if str(v) not in OUTPUT_TYPES}
            if bad:
                allowed = "str/int/bool/float"
                issues.append(
                    {
                        "code": "E_OUTPUTS",
                        "message": f"{node_id} 的输出字段类型非法: {bad}，只允许 {allowed}",
                    }
                )
                continue
            node["outputs"] = {str(k): str(v) for k, v in outputs.items()}
    return issues


def apply_bindings(
    draft: Draft, raw: list[dict[str, Any]], *, user_id: str
) -> list[dict[str, Any]]:
    """覆盖式提交工作流级绑定；私有绑定的 owner 固定为当前用户。"""
    issues: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw or []:
        kind = str(item.get("kind") or "")
        ref = str(item.get("ref") or "").strip()
        scope = str(item.get("scope") or "global")
        if kind not in {"mcp", "skill"}:
            issues.append({"code": "E_BINDING_KIND", "message": f"绑定类型不支持: {kind!r}"})
            continue
        if not ref:
            issues.append({"code": "E_BINDING_REF", "message": "绑定缺少 ref"})
            continue
        if scope not in {"global", "private"}:
            issues.append({"code": "E_BINDING_SCOPE", "message": f"绑定作用域不支持: {scope!r}"})
            continue
        key = (kind, ref)
        if key in seen:
            continue
        seen.add(key)
        binding: dict[str, Any] = {"kind": kind, "ref": ref, "scope": scope}
        if scope == "private":
            binding["owner"] = user_id
        merged.append(binding)
    if not issues:
        draft.bindings = merged
    return issues


def apply_triggers(
    draft: Draft,
    *,
    cron: str,
    timezone: str = "Asia/Shanghai",
    catchup_window_seconds: Any = 3600,
    catchup_limit: Any = 1,
    overlap_policy: Any = "skip",
) -> list[dict[str, Any]]:
    """覆盖式提交定时计划；cron 留空表示取消定时（回到只手动触发）。

    回填参数与重叠策略来自表单，可能是字符串或空值，这里统一收敛到合法范围，
    而不是把脏值写进草稿等发布时才炸。
    """
    expr = (cron or "").strip()
    if not expr:
        draft.triggers = []
        return []
    tz = (timezone or "Asia/Shanghai").strip()
    try:
        validate_cron(expr, tz)
    except ValueError as exc:
        return [{"code": "E_TRIGGER_CRON", "message": str(exc)}]
    policy = str(overlap_policy or "skip").strip()
    if policy not in {"skip", "buffer_one", "cancel_other"}:
        return [{"code": "E_TRIGGER_OVERLAP", "message": f"重叠策略不合法: {policy!r}"}]
    draft.triggers = [
        {
            "type": "cron",
            "cron": expr,
            "timezone": tz,
            "catchup_window_seconds": _int_in(
                catchup_window_seconds, 3600, low=0, high=30 * 24 * 3600
            ),
            "catchup_limit": _int_in(catchup_limit, 1, low=1, high=20),
            "overlap_policy": policy,
        }
    ]
    return []


def _int_in(raw: Any, default: int, *, low: int, high: int) -> int:
    """把表单里的任意值收敛到 [low, high]；解析不了就用默认值。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def editable_view(draft: Draft) -> list[dict[str, Any]]:
    """页面用的节点视图：只暴露可编辑字段与只读的标识信息。"""
    view = []
    for node in draft.nodes:
        prompt_file = draft_dir(draft.session_id) / f"prompts/{node['id']}.md"
        prompt_text = node.get("prompt")
        if prompt_text is None and prompt_file.exists():
            prompt_text = prompt_file.read_text(encoding="utf-8")
        view.append(
            {
                "id": node["id"],
                "title": node.get("title") or node["id"],
                "kind": node.get("kind") or "agent",
                "duty": node.get("duty") or "",
                "outputs": dict(node.get("outputs") or {}),
                "prompt": prompt_text or "",
            }
        )
    return view
