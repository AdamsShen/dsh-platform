"""创作面 A 期：对话 → 生成草稿 → 编辑节点 → 确认发布。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ao.planner import draft as draft_store
from ao.planner import session as planner_session
from ao.settings import settings
from ao.web import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "mock", True)
    return TestClient(create_app())


def _start(client: TestClient) -> str:
    resp = client.get("/planner/new", follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[-1]


def _node_form(nodes: list[dict]) -> dict[str, str]:
    data: dict[str, str] = {}
    for node in nodes:
        node_id = node["id"]
        data[f"title_{node_id}"] = node["title"]
        data[f"duty_{node_id}"] = node["duty"]
        data[f"outputs_{node_id}"] = json.dumps(node["outputs"])
        data[f"prompt_{node_id}"] = f"# {node['title']}\n\n人工改过的提示词：{node_id}\n"
    return data


def test_chat_to_draft_to_edited_publish(client: TestClient) -> None:
    session_id = _start(client)

    client.post(
        f"/planner/{session_id}/messages",
        data={"content": "每天早上 9 点给运营组生成昨日日报，输出 markdown"},
        follow_redirects=False,
    )
    client.post(f"/planner/{session_id}/generate", follow_redirects=False)

    page = client.get(f"/planner/{session_id}")
    assert page.status_code == 200
    assert "草稿节点" in page.text
    assert "collect" in page.text
    assert planner_session.get_session(session_id)["stage"] == "drafting"

    # 编辑两个字段后保存：包内提示词应立刻变成人工内容
    nodes = draft_store.editable_view(draft_store.Draft.load(session_id))
    nodes[0]["title"] = "取昨日数据"
    nodes[0]["duty"] = "从数仓取昨日订单与营收"
    resp = client.post(
        f"/planner/{session_id}/nodes",
        data=_node_form(nodes),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    prompt_file = draft_store.draft_dir(session_id) / "prompts/collect.md"
    assert "人工改过的提示词" in prompt_file.read_text(encoding="utf-8")
    manifest = json.loads(
        (draft_store.draft_dir(session_id) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["nodes"][0]["title"] == "取昨日数据"

    # 发布：同一表单提交，先落编辑再发布
    resp = client.post(
        f"/planner/{session_id}/publish",
        data=_node_form(nodes),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session = planner_session.get_session(session_id)
    assert session["stage"] == "published"
    assert session["workflow_id"]

    page = client.get(f"/planner/{session_id}")
    assert "已发布" in page.text
    assert session["workflow_id"] in page.text


def test_edit_rejects_bad_output_type(client: TestClient) -> None:
    session_id = _start(client)
    client.post(
        f"/planner/{session_id}/messages",
        data={"content": "每周一给老板生成周报"},
        follow_redirects=False,
    )
    client.post(f"/planner/{session_id}/generate", follow_redirects=False)

    nodes = draft_store.editable_view(draft_store.Draft.load(session_id))
    data = _node_form(nodes)
    data[f"outputs_{nodes[0]['id']}"] = json.dumps({"items": "list"})
    client.post(f"/planner/{session_id}/nodes", data=data, follow_redirects=False)

    # 非法类型不覆盖草稿，错误回落到会话上供页面展示
    session = planner_session.get_session(session_id)
    assert session["stage"] == "drafting"
    assert any("list" in issue["message"] for issue in session["draft_issues"])
    assert (
        draft_store.Draft.load(session_id).nodes[0]["outputs"]
        == nodes[0]["outputs"]
    )


def test_upload_document_feeds_slots(client: TestClient) -> None:
    session_id = _start(client)
    resp = client.post(
        f"/planner/{session_id}/uploads",
        files={"file": ("weekly.md", "# 周报要求".encode(), "text/markdown")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    session = planner_session.get_session(session_id)
    assert session["slots"]["documents"]
