"""P6：节点级提示词与 MCP/Skill 绑定（T01~T07）。

判定口径：
- T01 合并语义：工作流级是默认集，节点级只管自己（MCP 同名覆盖、Skill 并集）；
- T02 运行期按节点解析：不同节点的 `mcp_servers` 不同，且解析结果按节点缓存；
- T03 DSL 覆盖：`agent(mcp=[...])` 只影响本 turn，不污染节点绑定；
- T04 发布期定位：未知 ref 报错要带 `node`；
- T05/T06 system 与节点绑定进编辑面，保存即生效、manifest 不变；
- T07 端到端：三节点各绑不同工具，全程由 LangGraph 执行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ao.executor as executor_module
from ao import manifest as manifest_mod
from ao import resources
from ao.db import fetch_all, tx
from ao.executor import RunRuntime, execute
from ao.harness.mock import MockProvider
from ao.planner import draft as draft_store
from ao.planner import orchestrator
from ao.planner import session as planner_session
from ao.runs import create_run, ensure_workflow
from ao.settings import settings
from ao.web import create_app

PLAN = {
    "nodes": [
        {
            "id": "collect",
            "title": "取数",
            "duty": "拉取原始数据",
            "outputs": {"items": "str"},
        },
        {
            "id": "enrich",
            "title": "补全",
            "duty": "用人力库补全字段",
            "outputs": {"rows": "str"},
            "mcp": ["hr-db"],
        },
        {
            "id": "render",
            "title": "渲染",
            "duty": "把结果渲染成 PDF 与排版稿",
            "outputs": {"output": "str"},
            "mcp": ["hr-db", "pdf-render"],
            "skills": ["schedule-fmt"],
        },
    ],
    "edges": [{"from": "collect", "to": "enrich"}, {"from": "enrich", "to": "render"}],
}


class ScriptedModel:
    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        return PLAN


class RecordingProvider(MockProvider):
    """记录每次 turn 的 provider context：断言"每个节点到底注入了哪些工具"。"""

    def __init__(self) -> None:
        super().__init__(delay=0)
        self.contexts: dict[str, dict[str, Any]] = {}

    async def send(self, **kwargs: Any) -> Any:
        self.contexts[str(kwargs["node_id"])] = dict(kwargs.get("context") or {})
        return await super().send(**kwargs)


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "mock", True)
    return tmp_path


def _seed_resources() -> None:
    resources.upsert(
        kind="mcp", ref="hr-db", name="人力库", spec={"type": "stdio", "command": "hr"}
    )
    resources.upsert(
        kind="mcp", ref="pdf-render", name="PDF 渲染", spec={"type": "stdio", "command": "pdf"}
    )
    resources.upsert(
        kind="skill", ref="schedule-fmt", name="日程排版", spec={"description": "排版"}
    )


def _runtime(manifest: manifest_mod.Manifest, run_id: str) -> RunRuntime:
    with tx() as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES ('default','default')"
                     " ON CONFLICT DO NOTHING")
    ensure_workflow("wf_node_test", tenant_id="default", name="节点绑定测试")
    with tx() as conn:
        conn.execute(
            "INSERT INTO runs (id, tenant_id, workflow_id, version, package_ref, dedupe_key,"
            " subject, trigger_user_id, status, epoch)"
            " VALUES (%s,'default','wf_node_test',1,'/tmp/pkg',%s,'{}'::jsonb,'u_001','queued',1)"
            " ON CONFLICT (id) DO NOTHING",
            (run_id, run_id),
        )
    return RunRuntime(
        run={
            "id": run_id,
            "epoch": 1,
            "tenant_id": "default",
            "trigger_user_id": "u_001",
            "budget_usd": 0,
            "spent_usd": 0,
        },
        manifest=manifest,
        package=type("Pkg", (), {"root": Path("/tmp/pkg")})(),
        provider=MockProvider(delay=0),
    )


# --- T01 合并语义 -------------------------------------------------------------


def test_bindings_for_node_merges_override_and_union() -> None:
    parsed = manifest_mod.parse(
        {
            "id": "wf_node",
            "nodes": [
                {"id": "a"},
                {"id": "b", "mcp": ["hr-db", "crm-api"], "skills": ["weekly-report"]},
            ],
            "edges": [{"from": "a", "to": "b"}],
            "bindings": [
                # 私有绑定：节点级同名引用不能把它降级成 global
                {"kind": "mcp", "ref": "hr-db", "scope": "private", "owner": "u_001"},
                {"kind": "skill", "ref": "base-skill"},
            ],
        }
    )
    node_a = parsed.bindings_for_node(parsed.node("a"))
    assert [(b.kind, b.ref) for b in node_a] == [("mcp", "hr-db"), ("skill", "base-skill")]

    node_b = parsed.bindings_for_node(parsed.node("b"))
    assert [(b.kind, b.ref) for b in node_b] == [
        ("mcp", "hr-db"),
        ("skill", "base-skill"),
        ("mcp", "crm-api"),
        ("skill", "weekly-report"),
    ]
    hr = next(b for b in node_b if b.ref == "hr-db")
    assert (hr.scope, hr.owner) == ("private", "u_001")


def test_node_binding_limit_and_ref_format_are_validated() -> None:
    with pytest.raises(ValueError, match="超过上限"):
        manifest_mod.parse(
            {
                "id": "wf_many",
                "nodes": [{"id": "a", "mcp": [f"srv-{i}" for i in range(9)]}],
            }
        )
    with pytest.raises(ValueError, match="ref 不合法"):
        manifest_mod.parse({"id": "wf_bad_ref", "nodes": [{"id": "a", "mcp": ["HR DB"]}]})


# --- T02 运行期按节点解析 -----------------------------------------------------


def test_runtime_resolves_tools_per_node_and_caches(dirs: Path) -> None:
    _seed_resources()
    parsed = manifest_mod.parse(
        {
            "id": "wf_per_node",
            "nodes": [
                {"id": "a", "outputs": {"x": "str"}},
                {"id": "b", "mcp": ["hr-db"], "outputs": {"x": "str"}},
                {"id": "c", "mcp": ["hr-db", "pdf-render"], "outputs": {"x": "str"}},
            ],
            "bindings": [{"kind": "skill", "ref": "schedule-fmt"}],
        }
    )
    runtime = _runtime(parsed, "run_per_node")

    assert runtime._tools_for(parsed.node("a")) == ({}, ["schedule-fmt"])
    servers_b, skills_b = runtime._tools_for(parsed.node("b"))
    assert set(servers_b) == {"hr-db"} and skills_b == ["schedule-fmt"]
    servers_c, _ = runtime._tools_for(parsed.node("c"))
    assert set(servers_c) == {"hr-db", "pdf-render"}
    # 同一节点重复取用不重复解析（返回值是同一份缓存）
    assert runtime._tools_for(parsed.node("c")) is runtime._tools_for(parsed.node("c"))

    events = {
        row["node_id"]: row["payload"]
        for row in fetch_all(
            "SELECT node_id, payload FROM run_events"
            " WHERE run_id = %s AND type = 'binding.resolved'",
            ("run_per_node",),
        )
    }
    assert set(events) == {"a", "b", "c"}
    assert events["a"]["mcp"] == []
    assert events["b"]["mcp"] == ["hr-db"]
    assert events["c"]["mcp"] == ["hr-db", "pdf-render"]


# --- T03 DSL 覆盖只作用于本 turn ------------------------------------------------


async def test_dsl_override_only_affects_current_turn(dirs: Path) -> None:
    _seed_resources()
    parsed = manifest_mod.parse(
        {
            "id": "wf_override",
            "nodes": [{"id": "enrich", "outputs": {"rows": "str"}, "mcp": ["hr-db"]}],
        }
    )
    runtime = _runtime(parsed, "run_override")
    ctx = type(
        "Ctx",
        (),
        {"node_id": "enrich", "attempt": 1, "system": lambda self: ""},
    )()

    await runtime.call_agent(ctx, prompt="hi", vars={}, mcp=["pdf-render"], skills=None)
    sent = runtime.provider.calls[-1]
    # 断言的是真正注给 provider 的 context，不是缓存
    assert sent["node_id"] == "enrich"
    # 节点绑定没有被覆盖动作改写
    assert set(runtime._tools_for(parsed.node("enrich"))[0]) == {"hr-db"}


async def test_dsl_override_reaches_provider_context(dirs: Path) -> None:
    _seed_resources()
    parsed = manifest_mod.parse(
        {
            "id": "wf_override_ctx",
            "nodes": [{"id": "enrich", "outputs": {"rows": "str"}, "mcp": ["hr-db"]}],
        }
    )
    recorder = RecordingProvider()
    runtime = _runtime(parsed, "run_override_ctx")
    runtime.provider = recorder
    ctx = type(
        "Ctx",
        (),
        {"node_id": "enrich", "attempt": 1, "system": lambda self: ""},
    )()

    await runtime.call_agent(ctx, prompt="hi", vars={}, mcp=["pdf-render"], skills=None)
    assert set(recorder.contexts["enrich"]["mcp_servers"]) == {"pdf-render"}

    # 不传覆盖参数的 turn（如 repair）回到节点绑定
    await runtime._turn(ctx, prompt="again", repair=True)
    assert set(recorder.contexts["enrich"]["mcp_servers"]) == {"hr-db"}


# --- T04 发布期定位到节点 -----------------------------------------------------


def test_check_bindings_reports_node_id(dirs: Path) -> None:
    _seed_resources()
    parsed = manifest_mod.parse(
        {
            "id": "wf_check",
            "nodes": [{"id": "a"}, {"id": "b", "mcp": ["ghost-srv"]}],
            "bindings": [{"kind": "mcp", "ref": "also-ghost"}],
        }
    )
    issues = resources.check_bindings(parsed, user_id="u_001")
    by_node = {item.get("node"): item for item in issues}
    assert by_node[None]["code"] == "E_BINDING_MISSING"
    assert by_node["b"]["code"] == "E_BINDING_MISSING"
    assert "节点 b" in by_node["b"]["message"]
    assert "在该节点绑定里重新选择" in by_node["b"]["message"]


# --- T05 / T06 system 与节点绑定进编辑面 --------------------------------------


def test_web_node_card_saves_system_and_node_bindings(dirs: Path) -> None:
    _seed_resources()
    client = TestClient(create_app())
    session_id = (
        client.get("/planner/new", follow_redirects=False)
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    client.post(
        f"/planner/{session_id}/messages",
        data={"content": "取数后补全并渲染"},
        follow_redirects=False,
    )
    generated = client.post(f"/planner/{session_id}/generate", follow_redirects=False)
    assert generated.status_code == 303

    manifest_path = draft_store.draft_dir(session_id) / "manifest.json"
    before = manifest_path.read_bytes()

    form = {
        "title_collect": "取数",
        "duty_collect": "拉取原始数据",
        "outputs_collect": json.dumps({"items": "str"}),
        "prompt_collect": "拉数据",
        "system_collect": "你是取数节点，只返回数据。",
    }
    resp = client.post(
        f"/planner/{session_id}/nodes", data=form, follow_redirects=False
    )
    assert resp.status_code == 303

    draft = draft_store.Draft.load(session_id)
    node = next(item for item in draft.nodes if item["id"] == "collect")
    # 只改系统词：正文落在 prompts 文件里，manifest.json 一字节不变（hash 稳定）
    system_file = draft_store.draft_dir(session_id) / "prompts/collect.system.md"
    assert node["system"] == "你是取数节点，只返回数据。"
    assert system_file.read_text(encoding="utf-8") == "你是取数节点，只返回数据。"
    assert manifest_path.read_bytes() == before

    # 再勾节点级绑定：这时候 manifest 的 nodes[].mcp/skills 才该变
    resp = client.post(
        f"/planner/{session_id}/nodes",
        data={**form, "mcp_collect": ["hr-db"], "skills_collect": ["schedule-fmt"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    draft = draft_store.Draft.load(session_id)
    node = next(item for item in draft.nodes if item["id"] == "collect")
    assert node["mcp"] == ["hr-db"] and node["skills"] == ["schedule-fmt"]

    packaged = json.loads(manifest_path.read_text(encoding="utf-8"))
    collect = next(item for item in packaged["nodes"] if item["id"] == "collect")
    assert collect["mcp"] == ["hr-db"] and collect["skills"] == ["schedule-fmt"]
    # 未勾选 = 该节点不绑定，不继承（mock 模型的三节点是 collect/draft/finalize）
    untouched = next(item for item in packaged["nodes"] if item["id"] == "draft")
    assert untouched["mcp"] == untouched["skills"] == []

    page = client.get(f"/planner/{session_id}")
    assert 'name="system_collect"' in page.text
    assert 'name="mcp_collect"' in page.text
    assert 'name="skills_collect"' in page.text


async def test_update_draft_rejects_unknown_node_ref(dirs: Path) -> None:
    _seed_resources()
    session = planner_session.create_session(tenant_id="default", user_id="u_001")
    planner_session.append_message(session["id"], "user", "取数并渲染")
    prepared = await orchestrator.prepare(session["id"], model=ScriptedModel())
    assert prepared.ok, prepared.issues

    result = await orchestrator.update_draft(
        session["id"], [{"id": "collect", "mcp": ["ghost-srv"]}]
    )
    assert result.ok is False
    assert result.issues[0]["code"] == "E_BINDING_MISSING"
    assert result.issues[0]["node"] == "collect"


# --- T07 端到端：三节点各绑不同工具，仍是 LangGraph --------------------------


async def test_three_nodes_with_distinct_tools_run_through_langgraph(
    dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_resources()
    session = planner_session.create_session(tenant_id="default", user_id="u_001")
    planner_session.append_message(session["id"], "user", "取数、补全、渲染三步")
    prepared = await orchestrator.prepare(session["id"], model=ScriptedModel())
    assert prepared.ok, prepared.issues
    published = await orchestrator.publish_draft(session["id"], prepared=prepared)
    assert published.ok, published.issues

    # ① 产物仍是 wf_sdk DSL，由执行器编译成 StateGraph
    graph_text = (draft_store.draft_dir(session["id"]) / "graph.py").read_text(encoding="utf-8")
    assert "G = graph(nodes=[" in graph_text
    assert "StateGraph" not in graph_text  # 生成物不直接写 LangGraph，编译在 executor

    # ② 每个节点的工具集不同
    manifest = manifest_mod.parse(
        json.loads((draft_store.draft_dir(session["id"]) / "manifest.json").read_text("utf-8"))
    )
    assert [manifest.node(n).mcp for n in ("collect", "enrich", "render")] == [
        [],
        ["hr-db"],
        ["hr-db", "pdf-render"],
    ]

    run_id, created = create_run(
        tenant_id="default",
        workflow_id=prepared.workflow_id,
        version=1,
        package_ref=published.package_ref or "",
        subject={"user_id": "u_001"},
        window="w-node-bindings",
        trigger_user_id="u_001",
    )
    assert created

    recorder = RecordingProvider()
    monkeypatch.setattr(executor_module, "current_provider", lambda: recorder)
    assert await execute(run_id) == "succeeded"

    assert set(recorder.contexts["collect"]["mcp_servers"]) == set()
    assert set(recorder.contexts["enrich"]["mcp_servers"]) == {"hr-db"}
    assert set(recorder.contexts["render"]["mcp_servers"]) == {"hr-db", "pdf-render"}

    resolved = {
        row["node_id"]: row["payload"]
        for row in fetch_all(
            "SELECT node_id, payload FROM run_events"
            " WHERE run_id = %s AND type = 'binding.resolved'",
            (run_id,),
        )
    }
    assert resolved["collect"]["mcp"] == []
    assert resolved["enrich"]["mcp"] == ["hr-db"]
    assert resolved["render"]["mcp"] == ["hr-db", "pdf-render"]
    assert resolved["render"]["skills"] == ["schedule-fmt"]


def test_legacy_package_without_node_bindings_is_unchanged() -> None:
    """老包（节点级字段为空）：合并结果必须与改造前完全等价。"""
    legacy = manifest_mod.parse(
        {
            "id": "wf_legacy",
            "nodes": [{"id": "a", "outputs": {"x": "str"}}, {"id": "b"}],
            "edges": [{"from": "a", "to": "b"}],
            "bindings": [{"kind": "mcp", "ref": "documents"}],
        }
    )
    effective = [(b.kind, b.ref, b.scope) for b in legacy.effective_bindings()]
    for node in legacy.nodes:
        assert [(b.kind, b.ref, b.scope) for b in legacy.bindings_for_node(node)] == effective
