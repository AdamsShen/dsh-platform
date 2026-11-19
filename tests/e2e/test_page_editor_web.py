"""W46：编辑器页面——树可见、操作落盘、非法操作带回原因、预览可打开。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ao.planner import draft as draft_store
from ao.settings import settings
from ao.web import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "mock", True)
    return TestClient(create_app())


def _session() -> str:
    return draft_store.Draft(
        session_id="s_edit",
        workflow_id="wf_edit",
        name="编辑测试",
        tenant="default",
        requirement="看板",
        nodes=[],
        edges=[],
    ).save() or "s_edit"


def test_editor_renders_blocks_and_preview(client: TestClient) -> None:
    session_id = _session()
    page = client.get(f"/planner/{session_id}/pages/main")
    assert page.status_code == 200
    assert "页面结构" in page.text
    assert "属性面板" in page.text

    # 没编译过时预览 404，不现场编译（避免编辑页变成隐式构建入口）
    assert (
        client.get(f"/planner/{session_id}/pages/main/preview/index.html").status_code
        == 404
    )


def test_editor_add_column_then_preview(client: TestClient) -> None:
    session_id = _session()
    resp = client.post(
        f"/planner/{session_id}/pages/main/op",
        data={
            "op": "add_column",
            "target": "0",
            "field": "spent_usd",
            "label": "花费",
            "format": "number",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved = json.loads(
        (draft_store.draft_dir(session_id) / "ui" / "pages" / "main.json").read_text(
            encoding="utf-8"
        )
    )
    assert "spent_usd" in [col["field"] for col in saved["blocks"][0]["columns"]]

    preview = client.get(f"/planner/{session_id}/pages/main/preview/index.html")
    assert preview.status_code == 200
    assert "Content-Security-Policy" in preview.headers


def test_editor_reports_error_without_writing(client: TestClient) -> None:
    session_id = _session()
    resp = client.post(
        f"/planner/{session_id}/pages/main/op",
        data={"op": "add_column", "target": "0", "field": "x", "label": "X", "format": "evil"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert not (draft_store.draft_dir(session_id) / "ui" / "pages" / "main.json").exists()
