"""B 期：工作流级 MCP / Skill 绑定、资源权限、执行接线。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao import manifest as manifest_mod
from ao import resources
from ao.executor import RunRuntime
from ao.harness.mock import MockProvider
from ao.planner import draft as draft_store
from ao.planner import orchestrator
from ao.planner import session as planner_session
from ao.settings import settings
from ao.web import create_app

PLAN = {
    "nodes": [
        {
            "id": "collect",
            "title": "取数",
            "kind": "agent",
            "duty": "取数",
            "outputs": {"items": "str"},
        },
        {
            "id": "finalize",
            "title": "产出",
            "kind": "agent",
            "duty": "产出",
            "outputs": {"output": "str"},
        },
    ],
    "edges": [{"from": "collect", "to": "finalize"}],
}


class ScriptedModel:
    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        return PLAN


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "mock", True)
    return tmp_path


def _draft_session() -> str:
    session = planner_session.create_session(tenant_id="default", user_id="u_001")
    planner_session.append_message(session["id"], "user", "每天 9 点生成日报")
    return str(session["id"])


async def test_node_level_bindings_promoted_to_workflow() -> None:
    """老包只在节点上写了 mcp/skills：加载时并集提升为工作流级。"""
    parsed = manifest_mod.parse(
        {
            "id": "wf_legacy",
            "nodes": [
                {"id": "a", "mcp": ["documents"], "skills": ["weekly-report"]},
                {"id": "b"},
            ],
            "edges": [{"from": "a", "to": "b"}],
            "bindings": [{"kind": "mcp", "ref": "documents"}],
        }
    )
    assert [(b.kind, b.ref) for b in parsed.effective_bindings()] == [
        ("mcp", "documents"),
        ("skill", "weekly-report"),
    ]
    assert parsed.mcp_refs() == ["documents"]
    assert parsed.skill_refs() == ["weekly-report"]


async def test_private_binding_without_owner_is_rejected() -> None:
    with pytest.raises(ValueError, match="owner"):
        manifest_mod.parse(
            {
                "id": "wf_bad",
                "nodes": [{"id": "a"}],
                "bindings": [{"kind": "mcp", "ref": "x", "scope": "private"}],
            }
        )


async def test_publish_rejects_foreign_private_binding(dirs: Path) -> None:
    """别人的私有资源即使写进包，也不能发布。"""
    resources.upsert(
        kind="mcp",
        ref="secret-srv",
        name="他人私有",
        scope="private",
        owner_user_id="u_002",
        spec={"type": "stdio", "command": "secret"},
    )
    session_id = _draft_session()
    prepared = await orchestrator.prepare(session_id, model=ScriptedModel())
    assert prepared.ok, prepared.issues

    draft = draft_store.Draft.load(session_id)
    draft.bindings = [{"kind": "mcp", "ref": "secret-srv", "scope": "private", "owner": "u_002"}]
    draft.save()
    updated = await orchestrator.update_draft(session_id, [])
    assert updated.ok, updated.issues

    result = await orchestrator.publish_draft(session_id, prepared=prepared)
    assert result.ok is False
    assert result.stage == "bindings"
    assert result.issues[0]["code"] == "E_BINDING_FORBIDDEN"


async def test_executor_resolves_bindings_into_mcp_servers(dirs: Path) -> None:
    resources.upsert(
        kind="mcp",
        ref="demo-srv",
        name="演示 MCP",
        spec={"type": "stdio", "command": "demo-cmd"},
    )
    parsed = manifest_mod.parse(
        {
            "id": "wf_exec",
            "nodes": [{"id": "a", "outputs": {"x": "str"}}],
            "bindings": [{"kind": "mcp", "ref": "demo-srv"}],
        }
    )
    runtime = RunRuntime(
        run={
            "id": "run_x",
            "epoch": 1,
            "tenant_id": "default",
            "trigger_user_id": "u_001",
            "budget_usd": 0,
            "spent_usd": 0,
        },
        manifest=parsed,
        package=SimpleNamespace(root=dirs),
        provider=MockProvider(),
    )
    assert runtime.mcp_servers == {"demo-srv": {"type": "stdio", "command": "demo-cmd"}}
    assert runtime.skill_refs == []


def test_web_binding_selection_persists(dirs: Path) -> None:
    resources.upsert(
        kind="skill", ref="weekly-report", name="周报写作", spec={"description": "结论先行"}
    )
    client = TestClient(create_app())
    session_id = (
        client.get("/planner/new", follow_redirects=False)
        .headers["location"]
        .rsplit("/", 1)[-1]
    )
    client.post(
        f"/planner/{session_id}/messages",
        data={"content": "每天生成一份日报"},
        follow_redirects=False,
    )
    client.post(f"/planner/{session_id}/generate", follow_redirects=False)

    resp = client.post(
        f"/planner/{session_id}/bindings",
        data={"binding": ["skill:weekly-report"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    draft = draft_store.Draft.load(session_id)
    assert draft.bindings == [{"kind": "skill", "ref": "weekly-report", "scope": "global"}]
    packaged = json.loads(
        (draft_store.draft_dir(session_id) / "manifest.json").read_text(encoding="utf-8")
    )
    assert packaged["bindings"] == [
        {"kind": "skill", "ref": "weekly-report", "scope": "global"}
    ]
    page = client.get(f"/planner/{session_id}")
    assert 'value="skill:weekly-report"' in page.text
