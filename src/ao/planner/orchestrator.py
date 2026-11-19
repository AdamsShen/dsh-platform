"""P2-T12 生成编排：七个步骤把会话变成可发布的工作流包。

步骤（设计 §7.4）：
1 澄清 → 2 节点清单 → 3 manifest+提示词 → 4 graph.py+Page → 5 静态检查 → 6 冒烟 → 7 发布。

模型只在"节点清单"这一步做开放式判断，其余步骤都是确定性代码：
提示词由节点职责渲染、代码由骨架生成。这样同样输入能得到同样包，
失败也能定位到具体步骤，而不是"模型这次发挥不好"。

第 3-6 步失败（manifest/静态检查/冒烟）会带着具体错误回退，让模型重出清单，
最多 `settings.authoring_repair_rounds` 轮（默认 3）；发布失败不回退——那已不是清单能修的问题。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ao import codegen, resources, static_check
from ao import triggers as trigger_store
from ao.cron import validate_cron
from ao.manifest import NODE_BINDING_LIMIT, parse
from ao.package import freeze_package, load_package
from ao.pages import compiler as page_compiler
from ao.pages import schema as page_schema
from ao.pages.generate import generate_page, smoke_page
from ao.planner import draft as draft_store
from ao.planner import memory
from ao.planner import session as planner_session
from ao.publish import publish
from ao.runs import ensure_workflow, publish_version
from ao.settings import settings
from ao.smoke import run_smoke
from ao.util import new_id

logger = logging.getLogger(__name__)

STEPS = (
    (1, "clarify", "澄清需求"),
    (2, "nodes", "节点清单"),
    (3, "package", "生成 manifest 与提示词"),
    (4, "graph", "生成 graph.py 与页面"),
    (5, "static", "静态检查"),
    (6, "smoke", "冒烟"),
    (7, "publish", "发布"),
)
_RESULT_RE = re.compile(r"```result\s*(\{.*?\})\s*```", re.DOTALL)

#: 失败阶段 → 进度步号；绑定校验与静态检查同属发布前闸门
_STAGE_STEP = {
    "clarify": 1,
    "nodes": 2,
    "package": 3,
    "graph": 4,
    # 页面属于第 ④ 步的产物（graph.py + ui/），失败定位到同一步
    "page": 4,
    "static": 5,
    "bindings": 5,
    "smoke": 6,
    "publish": 7,
}


#: 平台开放的节点权限档位；full_access 不在开放集合内，不接受模型指定。
#: 实际隔离由 harness 原生 permission mode 落实（见 ao/harness/_dsh_runtime.py）。
NODE_CAPABILITIES = frozenset({"read_only", "workspace_write"})


def _step_no(stage: str) -> int:
    return _STAGE_STEP.get(stage, 0)


class Model(Protocol):
    """创作模型的最小接口：给一段提示词，返回结构化 JSON。"""

    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]: ...


class GenerationError(Exception):
    def __init__(self, stage: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(f"{stage}: {issues[:1]}")
        self.stage = stage
        self.issues = issues


@dataclass
class GenerationResult:
    ok: bool
    stage: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)
    workflow_id: str = ""
    version: int = 0
    manifest_hash: str = ""
    package_ref: str = ""
    draft_dir: str = ""
    #: 实际用掉的轮数（1 表示一次通过；>1 说明发生过修复）
    attempts: int = 1
    steps: list[dict[str, Any]] = field(default_factory=list)


class DshPlannerModel:
    """默认实现：走执行侧的 provider（真实模型或 mock），要求返回 ```result 块。"""

    def __init__(self, provider: Any = None, *, cwd: Path | None = None) -> None:
        self._provider = provider
        self._cwd = cwd or settings.data_dir

    def _get_provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        from ao.harness.dsh import DshProvider

        self._provider = DshProvider()
        return self._provider

    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        reply = await self._get_provider().send(
            run_id=f"authoring_{step}",
            node_id=step,
            attempt=0,
            system="你是工作流设计助手，只输出 ```result 代码块里的 JSON，不要解释。",
            prompt=prompt,
            contract=None,
            context={"cwd": str(self._cwd), "capability": "read_only", "timeout": 180},
        )
        if reply.status != "ok":
            raise GenerationError(step, [{"code": "E_MODEL", "message": reply.text[:300]}])
        match = _RESULT_RE.search(reply.text or "")
        if not match:
            raise GenerationError(step, [{"code": "E_MODEL_JSON", "message": reply.text[:300]}])
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise GenerationError(
                step, [{"code": "E_MODEL_JSON", "message": f"{exc}: {match.group(1)[:200]}"}]
            ) from exc
        if not isinstance(data, dict):
            raise GenerationError(step, [{"code": "E_MODEL_JSON", "message": "顶层必须是对象"}])
        return data


def _clarify(context: dict[str, Any]) -> list[str]:
    """槽位缺什么问什么；都齐了就不问，并写明显式假设。"""
    questions = []
    slots = context.get("slots") or {}
    labels = {
        "subject": "这条工作流服务谁（主体）？",
        "frequency": "多久跑一次（频率/触发时间）？",
        "outputs": "产出什么（文件/消息/表格）？",
        "systems": "要接哪些外部系统（钉钉/飞书/日历等）？",
    }
    for key, question in labels.items():
        if not memory.slot_values(slots, key):
            questions.append(question)
    return questions


def _available_for(user_id: str | None) -> list[dict[str, Any]]:
    """当前用户可见的 MCP / Skill 清单。

    读库失败不能让整条生成链挂掉：退化为"不注入候选"，发布期仍然会校验。
    """
    try:
        return resources.list_available(user_id=user_id)
    except Exception:  # noqa: BLE001 — 生成不依赖资源清单，缺了只是少一层提示
        logger.warning("读取资源清单失败，本次生成不注入候选绑定", exc_info=True)
        return []


def _check_draft_node_bindings(draft: Any, *, user_id: str | None) -> list[dict[str, Any]]:
    """保存草稿时就校验节点级绑定的存在性，不等发布。

    页面上的勾选项来自 `list_available`，正常路径选不出不存在的 ref；
    但 API / 生成器写的草稿可能带未知 ref，这里提前报错并指到节点。
    """
    issues: list[dict[str, Any]] = []
    for node in draft.nodes:
        for kind, refs in (
            ("mcp", node.get("mcp") or []),
            ("skill", node.get("skills") or []),
        ):
            for ref in refs:
                if resources.get(kind=kind, ref=str(ref), user_id=user_id) is not None:
                    continue
                issues.append(
                    {
                        "code": "E_BINDING_MISSING",
                        "node": node.get("id"),
                        "message": f"节点 {node.get('id')} 的 {kind} 绑定不存在: {ref}"
                        "（在该节点绑定里重新选择）",
                    }
                )
    return issues


def _nodes_prompt(requirement: str, available: list[dict[str, Any]] | None = None) -> str:
    """节点清单提示词。

    `available` 是用户可见的资源：只允许模型从这里挑 ref，避免臆造资源名。
    仍可能臆造——发布期 `E_BINDING_MISSING` 会拦截并定位到具体节点。
    """
    parts: list[str] = []
    mcp_refs = sorted({str(row["ref"]) for row in available or [] if row.get("kind") == "mcp"})
    skill_refs = sorted({str(row["ref"]) for row in available or [] if row.get("kind") == "skill"})
    if mcp_refs:
        parts.append("mcp 候选：" + "、".join(mcp_refs))
    if skill_refs:
        parts.append("skill 候选：" + "、".join(skill_refs))
    hint = ""
    if parts:
        hint = (
            "可用资源（只能引用这些 ref，不要臆造）：" + "；".join(parts) + "。\n"
            "某个节点需要与其他节点不同的工具时，在该节点上写 \"mcp\":[\"ref\"] 或 "
            "\"skills\":[\"ref\"]；各节点共用的工具留空，由工作流级默认绑定提供。\n"
        )
    return (
        "根据下面的需求，给出工作流节点清单。只输出 JSON：\n"
        '{"nodes":[{"id":"英文小写id","title":"中文标题",'
        '"duty":"一句话职责","outputs":{"字段名":"str|int|bool|float"},'
        '"mcp":["ref"],"skills":["ref"]}],'
        '"edges":[{"from":"a","to":"b"}],'
        '"schedule":{"cron":"分 时 日 月 周","timezone":"Asia/Shanghai"}}\n'
        "约束：节点 2-6 个；第一个是取数/准备，最后一个是产出；边默认线性。\n"
        f"{hint}"
        "需求里只要出现时间表达（如\"每天早上 8 点\"\"每周一 9:00\"），就必须输出 schedule；"
        "cron 五个字段依次是 分 时 日 月 周，时刻要与需求完全一致"
        "（需求说 8 点就写 8 点，不要按\"早上\"的习惯改成 9 点）；"
        "只有在需求完全没有时间表达时才省略 schedule。\n\n"
        f"需求：\n{requirement}"
    )


def _repair_prompt(plan: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    """把上一版清单和具体错误一起给模型：只说"失败了"它只会重抽一遍。"""
    problems = "\n".join(
        f"- {item.get('code')}: {item.get('message')}" for item in issues
    ) or "- 未知问题"
    return (
        "上一版节点清单没通过平台校验。请只输出修正后的节点清单 JSON（结构同前，不要解释）。\n"
        f"问题：\n{problems}\n\n"
        "硬约束：outputs 的值只能是 str/int/bool/float；节点 id 用小写字母、数字、下划线；"
        "边只能引用已声明的节点；节点 2-6 个。\n\n"
        f"上一版：\n{json.dumps(plan, ensure_ascii=False)}"
    )


def _require(condition: bool, stage: str, code: str, message: str) -> None:
    if not condition:
        raise GenerationError(stage, [{"code": code, "message": message}])


def _refs_of(raw: Any, node_id: str, kind: str) -> list[str]:
    """节点级绑定的 ref 归一：去空、去重、限个数。

    这里只做结构收敛（没有 DB 上下文，判断不了资源是否存在）：
    存在性由发布期 `check_bindings` 校验，并定位到具体节点。
    """
    if raw is None:
        return []
    _require(
        isinstance(raw, list),
        "nodes",
        "E_NODE_BINDING",
        f"{node_id} 的 {kind} 必须是数组",
    )
    refs = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    _require(
        len(refs) <= NODE_BINDING_LIMIT,
        "nodes",
        "E_NODE_BINDING",
        f"{node_id} 的 {kind} 绑定 {len(refs)} 个，超过上限 {NODE_BINDING_LIMIT}",
    )
    return refs


def _normalize_plan(
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    raw_nodes = plan.get("nodes")
    _require(isinstance(raw_nodes, list) and raw_nodes, "nodes", "E_NODES", "节点清单为空")
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_nodes):
        _require(isinstance(item, dict), "nodes", "E_NODES", f"第 {index} 个节点不是对象")
        node_id = str(item.get("id") or "").strip()
        _require(bool(node_id), "nodes", "E_NODES", f"第 {index} 个节点缺少 id")
        _require(node_id not in seen, "nodes", "E_NODES_DUP", f"节点 id 重复: {node_id}")
        seen.add(node_id)
        # action 节点已移除：模型若仍输出 kind=action（旧提示词/缓存），直接忽略按 agent 处理，
        # 不报错也不留 kind_requested——这个字段已无任何下游消费方
        # 节点可显式收紧权限；空值表示用平台默认（workspace_write）。非法值只告警不报错，
        # 否则模型随手写个 full_access 就会卡住整条生成链
        capability = str(item.get("capability") or "").strip()
        if capability and capability not in NODE_CAPABILITIES:
            logger.warning(
                "节点 %s 的 capability=%s 不在平台开放范围，已回落默认档位", node_id, capability
            )
            capability = ""
        outputs = item.get("outputs") or {}
        _require(
            isinstance(outputs, dict)
            and all(str(v) in {"str", "int", "bool", "float"} for v in outputs.values()),
            "nodes",
            "E_OUTPUTS",
            f"{node_id} 的 outputs 类型必须是 str/int/bool/float",
        )
        nodes.append(
            {
                "id": node_id,
                "title": str(item.get("title") or node_id),
                "duty": str(item.get("duty") or "").strip(),
                "outputs": {str(k): str(v) for k, v in outputs.items()},
                "mcp": _refs_of(item.get("mcp"), node_id, "mcp"),
                "skills": _refs_of(item.get("skills"), node_id, "skill"),
                # 空串 = 用平台默认 workspace_write
                "capability": capability,
            }
        )

    raw_edges = plan.get("edges") or []
    edges: list[dict[str, str]] = []
    if isinstance(raw_edges, list) and raw_edges:
        for item in raw_edges:
            if not isinstance(item, dict):
                continue
            src, dst = str(item.get("from") or ""), str(item.get("to") or "")
            _require(
                src in seen and dst in seen,
                "nodes",
                "E_EDGE",
                f"边引用了不存在的节点: {item}",
            )
            edge = {"from": src, "to": dst}
            if item.get("when"):
                edge["when"] = str(item["when"])
            edges.append(edge)
    else:
        edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]

    # 计划里可选的 schedule：模型识别出"每天 9 点"这类节奏就带上，没有就只手动触发
    triggers: list[dict[str, Any]] = []
    schedule = plan.get("schedule")
    if isinstance(schedule, dict):
        cron = str(schedule.get("cron") or "").strip()
        if cron:
            timezone = str(schedule.get("timezone") or "Asia/Shanghai").strip()
            try:
                validate_cron(cron, timezone)
            except ValueError as exc:
                _require(False, "nodes", "E_TRIGGER", f"schedule 不合法: {exc}")
            triggers.append({"type": "cron", "cron": cron, "timezone": timezone})
    return nodes, edges, triggers


def _build_manifest(
    *,
    workflow_id: str,
    name: str,
    tenant: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    bindings: list[dict[str, Any]] | None = None,
    triggers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_version": 2,
        "id": workflow_id,
        "version": 1,
        "name": name,
        "tenant": tenant,
        "limits": {"max_nodes": 12, "max_turns": 60, "max_loops": 3, "max_wall_seconds": 900},
        "vars": {},
        "nodes": [
            {
                "id": node["id"],
                "kind": "agent",
                "title": node["title"],
                "prompt": f"prompts/{node['id']}.md",
                "system": f"prompts/{node['id']}.system.md",
                # 契约由 outputs 内联表达（执行器就是这么读的），不额外产出 contracts/ 文件
                "outputs": node["outputs"],
                # 节点级工具绑定：只影响该节点；空表示完全继承工作流级 bindings
                "mcp": list(node.get("mcp") or []),
                "skills": list(node.get("skills") or []),
                # 权限档位与执行方式解耦：agent 节点也要能往 workspace/artifacts 落文件。
                # 隔离交给 harness 的 workspace-write：可写范围限于工作目录，prompts/contracts
                # 不在其中，因此默认给 workspace_write 不等于放权；
                # 节点可用 capability 收紧到 read_only
                "capability": node.get("capability") or "workspace_write",
                "retry": {"max": 1, "backoff": 10.0},
                "timeout": 300,
            }
            for node in nodes
        ],
        "edges": edges,
        "triggers": triggers or [{"type": "manual"}],
        "bindings": bindings or [],
        "ui": {"pages": "ui/pages/main.json"},
    }


def _write_package(
    draft: Path, manifest: dict[str, Any], nodes: list[dict[str, Any]], *, requirement: str
) -> None:
    draft.mkdir(parents=True, exist_ok=True)
    for name in ("prompts", "ui/pages", "tests"):
        (draft / name).mkdir(parents=True, exist_ok=True)

    # 修复后节点可能改名/删除：清掉上一版残留的提示词，否则包 hash 与内容都会漂
    wanted = {f"{node['id']}.md" for node in nodes} | {
        f"{node['id']}.system.md" for node in nodes
    }
    for stale in (draft / "prompts").glob("*"):
        if stale.name not in wanted:
            stale.unlink()

    (draft / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for node in nodes:
        # 人工编辑过的提示词正文优先；没有才按职责渲染默认模板
        body = node.get("prompt") or (
            f"# {node['title']}\n\n职责：{node['duty']}\n\n已确认需求：\n{requirement}\n"
        )
        (draft / f"prompts/{node['id']}.md").write_text(body, encoding="utf-8")
        # 用户改过系统词就落用户的版本，没改才写默认模板；
        # 单调性要求：同一份草稿反复重建包，这两个文件的字节必须一致（否则 hash 会漂）
        system_body = node.get("system")
        if system_body is None:
            system_body = "你是一个只负责本节点职责的执行者；按契约输出 result 块，不解释过程。\n"
        (draft / f"prompts/{node['id']}.system.md").write_text(system_body, encoding="utf-8")
    (draft / "ui/pages/main.json").write_text(
        json.dumps({"page": "main", "nodes": [n["id"] for n in nodes]}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (draft / "tests/smoke.py").write_text(
        '"""冒烟由平台 run_smoke 执行；此文件用于包内自描述。"""\n', encoding="utf-8"
    )


def _materialize_page(
    draft: Path, *, workflow_id: str, title: str
) -> list[str]:
    """第 ④ 步的页面半程：生成 Schema → 校验 → 编译 → 冒烟。

    失败抛 `GenerationError('page')`，由生成循环把缺失组件回喂模型重出清单；
    页面是确定性推导，正常不会进重试分支，但契约变更（比如组件被删）要能定位。
    """
    page = generate_page(workflow_id=workflow_id, title=title)
    try:
        clean = page_schema.load(page)
    except page_schema.PageSchemaError as exc:
        raise GenerationError("page", [issue.as_dict() for issue in exc.issues]) from exc
    (draft / "ui" / "pages" / "main.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    page_compiler.write_page(clean, draft / "ui" / "compiled" / "main")
    issues = smoke_page(draft, "main")
    if issues:
        raise GenerationError("page", issues)
    return ["ui/compiled/main/index.html", "ui/compiled/main/app.js", "ui/compiled/main/theme.css"]


def _artifact(session_id: str, step: int, name: str, status: str, payload: dict[str, Any]) -> None:
    planner_session.append_message(
        session_id,
        "artifact",
        f"第 {step} 步 {name}：{status}",
        payload={"step": step, "name": name, "status": status, **payload},
    )


async def generate(
    session_id: str, *, model: Model | None = None, tenant_id: str | None = None
) -> GenerationResult:
    session = planner_session.require_session(session_id)
    context = planner_session.build_context(session_id, recent=12)
    tenant = tenant_id or session["tenant_id"]
    requirement = (context["summary"] + "\n" + context["slots_text"]).strip()
    draft = settings.data_dir / "drafts" / session_id
    model = model or DshPlannerModel()
    steps: list[dict[str, Any]] = []
    result = GenerationResult(ok=False, draft_dir=str(draft), steps=steps)

    def done(
        step: int, name: str, payload: dict[str, Any] | None = None, *, attempt: int = 1
    ) -> None:
        steps.append({"step": step, "name": name, "status": "ok", "attempt": attempt})
        _artifact(session_id, step, name, "ok", {"attempt": attempt, **(payload or {})})

    def failed(step: int, name: str, issues: list[dict[str, Any]], *, attempt: int) -> None:
        steps.append(
            {"step": step, "name": name, "status": "failed", "attempt": attempt, "issues": issues}
        )
        _artifact(session_id, step, name, "failed", {"attempt": attempt, "issues": issues})

    max_rounds = max(1, settings.authoring_repair_rounds)

    try:
        # 1 澄清：只问缺的槽位，明确记录"按默认假设"的项
        questions = _clarify(context)
        done(1, "clarify", {"questions": questions})

        # 2 节点清单（唯一调用模型的步骤）
        plan = await model.generate(
            step="nodes",
            prompt=_nodes_prompt(requirement, _available_for(session["user_id"])),
        )
        nodes, edges, triggers = _normalize_plan(plan)
        done(2, "nodes", {"nodes": [n["id"] for n in nodes]})

        workflow_id = new_id("wf")
        manifest: dict[str, Any] = {}
        parsed: Any = None

        # 3-6 一次构建 + 校验；失败把错误回喂模型重出清单，最多 max_rounds 轮
        for attempt in range(1, max_rounds + 1):
            result.attempts = attempt
            try:
                manifest = _build_manifest(
                    workflow_id=workflow_id,
                    name=f"{session['user_id']} 的工作流",
                    tenant=tenant,
                    nodes=nodes,
                    edges=edges,
                    triggers=triggers,
                )
                _write_package(draft, manifest, nodes, requirement=requirement)
                try:
                    parsed = parse(manifest)
                except ValueError as exc:
                    raise GenerationError(
                        "package", [{"code": "E_MANIFEST", "message": str(exc)}]
                    ) from exc
                done(
                    3,
                    "package",
                    {"files": ["manifest.json"] + [f"prompts/{n['id']}.md" for n in nodes]},
                    attempt=attempt,
                )

                (draft / "graph.py").write_text(codegen.generate_graph(parsed), encoding="utf-8")
                page_files = _materialize_page(
                    draft, workflow_id=workflow_id, title=f"{session['user_id']} 的工作流"
                )
                done(
                    4,
                    "graph",
                    {"files": ["graph.py", "ui/pages/main.json", *page_files]},
                    attempt=attempt,
                )

                issues = static_check.check_file(draft / "graph.py", parsed)
                if issues:
                    raise GenerationError("static", [issue.as_dict() for issue in issues])
                done(5, "static", {"issues": []}, attempt=attempt)

                # 冒烟用 mock，确定性；失败通常意味着契约或依赖配错
                smoke = await run_smoke(draft)
                if not smoke["ok"]:
                    raise GenerationError(
                        "smoke",
                        [
                            {
                                "code": "E_SMOKE",
                                "message": smoke.get("error") or "冒烟未通过",
                                "nodes": [
                                    n for n in smoke["nodes"] if n["status"] != "succeeded"
                                ],
                            }
                        ],
                    )
                done(6, "smoke", {"run_id": smoke.get("run_id")}, attempt=attempt)
                break
            except GenerationError as exc:
                step_no = _step_no(exc.stage) or 3
                failed(step_no, exc.stage, exc.issues, attempt=attempt)
                if attempt >= max_rounds:
                    raise
                plan = await model.generate(
                    step="repair", prompt=_repair_prompt(plan, exc.issues)
                )
                nodes, edges, triggers = _normalize_plan(plan)
                done(2, "repair", {"nodes": [n["id"] for n in nodes]}, attempt=attempt + 1)

        if parsed is None:
            raise GenerationError("package", [{"code": "E_MANIFEST", "message": "未生成 manifest"}])

        # 7 发布：这里不再重复冒烟（第 6 步刚跑过）
        published = await publish(draft, smoke=False, user_id=session["user_id"])
        if not published.ok:
            raise GenerationError(published.stage, published.issues)
        manifest_hash, _ = freeze_package(draft)
        load_package(draft)
        # 冒烟阶段写的是 draft 版本行，这里把它升成 published（同一 (workflow, version) 覆盖）
        ensure_workflow(workflow_id, tenant_id=tenant, name=manifest["name"])
        publish_version(
            workflow_id,
            version=1,
            manifest_hash=manifest_hash,
            package_ref=published.package_ref or str(draft),
            status="published",
        )
        # 发布即切换：旧计划失效，新计划从下一个未来时刻开始
        trigger_store.project(
            workflow_id=workflow_id,
            version=1,
            tenant_id=tenant,
            owner_user_id=session["user_id"],
            package_ref=published.package_ref or str(draft),
            triggers=parsed.triggers,
        )
        planner_session.bind_workflow(session_id, workflow_id)
        done(
            7,
            "publish",
            {"manifest_hash": manifest_hash, "package_ref": published.package_ref},
            attempt=result.attempts,
        )

        result.ok = True
        result.stage = "published"
        result.workflow_id = workflow_id
        result.version = 1
        result.manifest_hash = manifest_hash
        result.package_ref = published.package_ref or ""
        return result
    except GenerationError as exc:
        result.stage = exc.stage
        result.issues = exc.issues
        step_no = _step_no(exc.stage)
        failed(step_no, exc.stage, exc.issues, attempt=result.attempts)
        return result


async def prepare(
    session_id: str, *, model: Model | None = None, tenant_id: str | None = None
) -> GenerationResult:
    """只生成到草稿（第 1-5 步），不发布；草稿可在页面上人工编辑后再发。

    与 `generate()` 的差别：这里把 3-5 步的修复循环保留（清单类问题仍让模型改），
    但停在静态检查通过为止——冒烟与发布交给 `publish_draft()`，
    避免"人刚改完就被自动修复覆盖"。
    """
    session = planner_session.require_session(session_id)
    context = planner_session.build_context(session_id, recent=12)
    tenant = tenant_id or session["tenant_id"]
    requirement = (context["summary"] + "\n" + context["slots_text"]).strip()
    target = draft_store.draft_dir(session_id)
    model = model or DshPlannerModel()
    steps: list[dict[str, Any]] = []
    result = GenerationResult(ok=False, draft_dir=str(target), steps=steps)
    max_rounds = max(1, settings.authoring_repair_rounds)

    def done(
        step: int, name: str, payload: dict[str, Any] | None = None, *, attempt: int = 1
    ) -> None:
        steps.append({"step": step, "name": name, "status": "ok", "attempt": attempt})
        _artifact(session_id, step, name, "ok", {"attempt": attempt, **(payload or {})})

    def failed(step: int, name: str, issues: list[dict[str, Any]], *, attempt: int) -> None:
        steps.append(
            {"step": step, "name": name, "status": "failed", "attempt": attempt, "issues": issues}
        )
        _artifact(session_id, step, name, "failed", {"attempt": attempt, "issues": issues})

    try:
        questions = _clarify(context)
        done(1, "clarify", {"questions": questions})

        plan = await model.generate(
            step="nodes",
            prompt=_nodes_prompt(requirement, _available_for(session["user_id"])),
        )
        nodes, edges, triggers = _normalize_plan(plan)
        done(2, "nodes", {"nodes": [n["id"] for n in nodes]})

        workflow_id = new_id("wf")
        for attempt in range(1, max_rounds + 1):
            result.attempts = attempt
            try:
                manifest = _build_manifest(
                    workflow_id=workflow_id,
                    name=f"{session['user_id']} 的工作流",
                    tenant=tenant,
                    nodes=nodes,
                    edges=edges,
                    triggers=triggers,
                )
                _write_package(target, manifest, nodes, requirement=requirement)
                try:
                    parsed = parse(manifest)
                except ValueError as exc:
                    raise GenerationError(
                        "package", [{"code": "E_MANIFEST", "message": str(exc)}]
                    ) from exc
                done(
                    3,
                    "package",
                    {"files": ["manifest.json"] + [f"prompts/{n['id']}.md" for n in nodes]},
                    attempt=attempt,
                )

                (target / "graph.py").write_text(codegen.generate_graph(parsed), encoding="utf-8")
                page_files = _materialize_page(
                    target, workflow_id=workflow_id, title=f"{session['user_id']} 的工作流"
                )
                done(
                    4,
                    "graph",
                    {"files": ["graph.py", "ui/pages/main.json", *page_files]},
                    attempt=attempt,
                )

                problems = static_check.check_file(target / "graph.py", parsed)
                if problems:
                    raise GenerationError("static", [issue.as_dict() for issue in problems])
                done(5, "static", {"issues": []}, attempt=attempt)
                break
            except GenerationError as exc:
                step_no = _step_no(exc.stage) or 3
                failed(step_no, exc.stage, exc.issues, attempt=attempt)
                if attempt >= max_rounds:
                    raise
                plan = await model.generate(step="repair", prompt=_repair_prompt(plan, exc.issues))
                nodes, edges, triggers = _normalize_plan(plan)
                done(2, "repair", {"nodes": [n["id"] for n in nodes]}, attempt=attempt + 1)

        draft_store.Draft(
            session_id=session_id,
            workflow_id=workflow_id,
            name=f"{session['user_id']} 的工作流",
            tenant=tenant,
            requirement=requirement,
            nodes=nodes,
            edges=edges,
            triggers=triggers,
            attempts=result.attempts,
        ).save()
        planner_session.set_stage(session_id, "drafting")
        result.ok = True
        result.stage = "drafting"
        result.workflow_id = workflow_id
        return result
    except GenerationError as exc:
        result.stage = exc.stage
        result.issues = exc.issues
        step_no = _step_no(exc.stage)
        failed(step_no, exc.stage, exc.issues, attempt=result.attempts)
        planner_session.set_stage(session_id, "chatting", exc.issues)
        return result


async def update_draft(
    session_id: str,
    edits: list[dict[str, Any]],
    *,
    bindings: list[dict[str, Any]] | None = None,
    schedule: dict[str, Any] | None = None,
) -> GenerationResult:
    """合并人工编辑、绑定与定时计划并重建包；只校验结构，不冒烟、不发布。"""
    draft = draft_store.Draft.load(session_id)
    session = planner_session.require_session(session_id)
    target = draft_store.draft_dir(session_id)
    steps: list[dict[str, Any]] = []
    result = GenerationResult(
        ok=False,
        draft_dir=str(target),
        steps=steps,
        workflow_id=draft.workflow_id,
        attempts=draft.attempts,
    )

    issues = draft_store.apply_edits(draft, edits)
    if bindings is not None:
        issues.extend(draft_store.apply_bindings(draft, bindings, user_id=session["user_id"]))
    if schedule is not None:
        issues.extend(
            draft_store.apply_triggers(
                draft,
                cron=str(schedule.get("cron") or ""),
                timezone=str(schedule.get("timezone") or "Asia/Shanghai"),
                catchup_window_seconds=schedule.get("catchup_window_seconds", 3600),
                catchup_limit=schedule.get("catchup_limit", 1),
                overlap_policy=schedule.get("overlap_policy", "skip"),
            )
        )
    if not issues:
        issues.extend(_check_draft_node_bindings(draft, user_id=session["user_id"]))
    if issues:
        result.stage = "draft"
        result.issues = issues
        planner_session.set_stage(session_id, "drafting", issues)
        return result

    try:
        manifest = _build_manifest(
            workflow_id=draft.workflow_id,
            name=draft.name,
            tenant=draft.tenant,
            nodes=draft.nodes,
            edges=draft.edges,
            bindings=draft.bindings,
            triggers=draft.triggers,
        )
        _write_package(target, manifest, draft.nodes, requirement=draft.requirement)
        try:
            parsed = parse(manifest)
        except ValueError as exc:
            raise GenerationError(
                "package", [{"code": "E_MANIFEST", "message": str(exc)}]
            ) from exc
        (target / "graph.py").write_text(codegen.generate_graph(parsed), encoding="utf-8")
        problems = static_check.check_file(target / "graph.py", parsed)
        if problems:
            raise GenerationError("static", [issue.as_dict() for issue in problems])
    except GenerationError as exc:
        result.stage = exc.stage
        result.issues = exc.issues
        # 编辑没通过校验时草稿保持上一版可用状态，不落盘覆盖
        planner_session.set_stage(session_id, "drafting", exc.issues)
        return result

    draft.save()
    planner_session.set_stage(session_id, "drafting", [])
    result.ok = True
    result.stage = "drafting"
    return result


async def publish_draft(
    session_id: str, *, prepared: GenerationResult | None = None
) -> GenerationResult:
    """把当前草稿跑冒烟并发布（第 6-7 步）；静态检查失败会拦在这里。

    这里**不做**自动修复：草稿可能已经被人改过，模型重写会覆盖人的编辑。
    """
    draft = draft_store.Draft.load(session_id)
    target = draft_store.draft_dir(session_id)
    steps: list[dict[str, Any]] = list(prepared.steps) if prepared else []
    result = GenerationResult(
        ok=False,
        draft_dir=str(target),
        steps=steps,
        workflow_id=draft.workflow_id,
        attempts=prepared.attempts if prepared else draft.attempts,
    )
    planner_session.set_stage(session_id, "publishing")

    def done(step: int, name: str, payload: dict[str, Any] | None = None, *, attempt: int) -> None:
        steps.append({"step": step, "name": name, "status": "ok", "attempt": attempt})
        _artifact(session_id, step, name, "ok", {"attempt": attempt, **(payload or {})})

    def failed(step: int, name: str, issues: list[dict[str, Any]], *, attempt: int) -> None:
        steps.append(
            {"step": step, "name": name, "status": "failed", "attempt": attempt, "issues": issues}
        )
        _artifact(session_id, step, name, "failed", {"attempt": attempt, "issues": issues})

    try:
        try:
            manifest = parse(json.loads((target / "manifest.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise GenerationError(
                "package", [{"code": "E_MANIFEST", "message": str(exc)}]
            ) from exc

        problems = static_check.check_file(target / "graph.py", manifest)
        if problems:
            raise GenerationError("static", [issue.as_dict() for issue in problems])

        smoke = await run_smoke(target)
        if not smoke["ok"]:
            raise GenerationError(
                "smoke",
                [
                    {
                        "code": "E_SMOKE",
                        "message": smoke.get("error") or "冒烟未通过",
                        "nodes": [n for n in smoke["nodes"] if n["status"] != "succeeded"],
                    }
                ],
            )
        done(6, "smoke", {"run_id": smoke.get("run_id")}, attempt=result.attempts)

        user_id = planner_session.require_session(session_id)["user_id"]
        published = await publish(target, smoke=False, user_id=user_id)
        if not published.ok:
            raise GenerationError(published.stage, published.issues)
        manifest_hash, _ = freeze_package(target)
        load_package(target)
        ensure_workflow(draft.workflow_id, tenant_id=draft.tenant, name=draft.name)
        publish_version(
            draft.workflow_id,
            version=1,
            manifest_hash=manifest_hash,
            package_ref=published.package_ref or str(target),
            status="published",
        )
        # 发布即切换：旧计划失效，新计划从下一个未来时刻开始
        trigger_store.project(
            workflow_id=draft.workflow_id,
            version=1,
            tenant_id=draft.tenant,
            owner_user_id=user_id,
            package_ref=published.package_ref or str(target),
            triggers=manifest.triggers,
        )
        planner_session.bind_workflow(session_id, draft.workflow_id)
        done(
            7,
            "publish",
            {"manifest_hash": manifest_hash, "package_ref": published.package_ref},
            attempt=result.attempts,
        )

        planner_session.set_stage(session_id, "published", [])
        result.ok = True
        result.stage = "published"
        result.version = 1
        result.manifest_hash = manifest_hash
        result.package_ref = published.package_ref or ""
        return result
    except GenerationError as exc:
        result.stage = exc.stage
        result.issues = exc.issues
        step_no = _step_no(exc.stage)
        failed(step_no, exc.stage, exc.issues, attempt=result.attempts)
        planner_session.set_stage(session_id, "failed", exc.issues)
        return result
