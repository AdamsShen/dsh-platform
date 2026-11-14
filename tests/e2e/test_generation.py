"""P2-T12 七步生成：mock 模型走完全流程，并验证静态检查失败的落点。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ao import planner
from ao.db import fetch_all, fetch_one
from ao.package import load_package
from ao.planner import orchestrator
from ao.settings import settings

PLAN = {
    "nodes": [
        {
            "id": "collect",
            "title": "取数",
            "kind": "agent",
            "duty": "拉取用户角色卡与日程",
            "outputs": {"items": "str"},
            "mcp": ["mcp__roles__get"],
        },
        {
            "id": "draft",
            "title": "生成日程",
            "kind": "agent",
            "duty": "按角色与心情生成次日安排",
            "outputs": {"plan": "str"},
        },
        {
            "id": "finalize",
            "title": "整理交付",
            "kind": "agent",
            "duty": "汇总并输出日报",
            "outputs": {"summary": "str"},
        },
    ],
    "edges": [{"from": "collect", "to": "draft"}, {"from": "draft", "to": "finalize"}],
}


class ScriptedModel:
    """确定性替身：记录被调用的步骤与提示词，返回固定计划。"""

    def __init__(self, plan: dict[str, Any], repair_plan: dict[str, Any] | None = None) -> None:
        self.plan = plan
        self.repair_plan = repair_plan or plan
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        self.calls.append(step)
        self.prompts.append(prompt)
        return self.repair_plan if step == "repair" else self.plan


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """草稿与包仓库都指到临时目录，避免污染仓库。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    return tmp_path


async def test_generation_runs_seven_steps_and_publishes(isolated_dirs: Path) -> None:
    session = planner.session.create_session(tenant_id="default", user_id="u_001")
    planner.session.append_message(
        session["id"], "user", "每小时给小林生成日报，输出 markdown"
    )
    model = ScriptedModel(PLAN)

    result = await orchestrator.generate(session["id"], model=model)

    assert result.ok, (result.stage, result.issues)
    assert model.calls == ["nodes"]  # 只有这一步需要模型判断

    # 包内文件齐全，且 manifest 与计划一致
    package = load_package(result.package_ref)
    manifest = json.loads((package.root / "manifest.json").read_text(encoding="utf-8"))
    assert [node["id"] for node in manifest["nodes"]] == ["collect", "draft", "finalize"]
    assert manifest["edges"] == PLAN["edges"]
    for node in manifest["nodes"]:
        assert (package.root / node["prompt"]).exists()
        assert (package.root / node["system"]).exists()
    assert (package.root / "graph.py").exists()
    assert (package.root / "ui/pages/main.json").exists()

    # 七步都落了 artifact，前端可按步展示
    artifacts = fetch_all(
        "SELECT payload FROM planner_messages WHERE session_id = %s AND role = 'artifact'"
        " ORDER BY id",
        (session["id"],),
    )
    assert [item["payload"]["step"] for item in artifacts] == [1, 2, 3, 4, 5, 6, 7]
    assert all(item["payload"]["status"] == "ok" for item in artifacts)

    # 版本行是 published，会话回填了 workflow_id
    version = fetch_one(
        "SELECT status, manifest_hash, package_ref FROM workflow_versions WHERE workflow_id = %s",
        (result.workflow_id,),
    )
    assert version is not None
    assert version["status"] == "published"
    assert version["manifest_hash"] == result.manifest_hash
    stored = planner.session.get_session(session["id"])
    assert stored["workflow_id"] == result.workflow_id


async def test_static_check_failure_keeps_draft_and_blocks_publish(
    isolated_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeIssue:
        def as_dict(self) -> dict[str, Any]:
            return {"code": "E_IMPORT", "message": "禁止 import os", "line": 3}

    monkeypatch.setattr(orchestrator.static_check, "check_file", lambda *a, **k: [FakeIssue()])

    session = planner.session.create_session(tenant_id="default", user_id="u_001")
    planner.session.append_message(session["id"], "user", "每天 9:00 生成周报")

    model = ScriptedModel(PLAN)
    result = await orchestrator.generate(session["id"], model=model)

    assert result.ok is False
    assert result.stage == "static"
    assert result.issues[0]["code"] == "E_IMPORT"
    # 回退轮数用尽：首次 + 2 次修复（默认 3 轮），每次修复都把错误带回去
    assert result.attempts == 3
    assert model.calls == ["nodes", "repair", "repair"]
    assert all("禁止 import os" in prompt for prompt in model.prompts[1:])

    # 草稿保留（供人下载/排查），但没有发布版本，会话也不绑定工作流
    draft = Path(result.draft_dir)
    assert (draft / "manifest.json").exists()
    assert (draft / "graph.py").exists()
    assert planner.session.get_session(session["id"])["workflow_id"] is None
    assert (
        fetch_one(
            "SELECT COUNT(*) AS n FROM workflow_versions WHERE package_ref LIKE %s",
            (f"%{draft.name}%",),
        )["n"]
        == 0
    )


async def test_repair_round_recovers_and_publishes(
    isolated_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一次静态检查失败、第二次通过：应停在 2 轮并正常发布。"""

    class FakeIssue:
        def as_dict(self) -> dict[str, Any]:
            return {"code": "E_IMPORT", "message": "禁止 import os", "line": 3}

    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> list[Any]:
        calls["n"] += 1
        return [FakeIssue()] if calls["n"] == 1 else []

    monkeypatch.setattr(orchestrator.static_check, "check_file", flaky)

    session = planner.session.create_session(tenant_id="default", user_id="u_001")
    planner.session.append_message(session["id"], "user", "每天 9:00 生成周报")
    model = ScriptedModel(PLAN)

    result = await orchestrator.generate(session["id"], model=model)

    assert result.ok, (result.stage, result.issues)
    assert result.attempts == 2
    assert model.calls == ["nodes", "repair"]
    assert "禁止 import os" in model.prompts[1]

    artifacts = fetch_all(
        "SELECT payload FROM planner_messages WHERE session_id = %s AND role = 'artifact'"
        " ORDER BY id",
        (session["id"],),
    )
    marks = [
        (item["payload"]["step"], item["payload"]["status"], item["payload"]["attempt"])
        for item in artifacts
    ]
    assert (5, "failed", 1) in marks  # 第一轮静态检查失败
    assert (5, "ok", 2) in marks  # 第二轮通过
    assert (7, "ok", 2) in marks  # 发布发生在第二轮

    version = fetch_one(
        "SELECT status FROM workflow_versions WHERE workflow_id = %s",
        (result.workflow_id,),
    )
    assert version is not None and version["status"] == "published"
