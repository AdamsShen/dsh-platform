"""W48：免登录分享——未登录可读、动作 403、scope 外 404、撤销/过期/超次。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao import share as share_store
from ao.db import tx
from ao.package import freeze_package
from ao.pages import compiler
from ao.runs import create_run, ensure_workflow, publish_version
from ao.store import publish as store_publish
from ao.util import new_id
from ao.web import create_app

SCHEMA: dict[str, Any] = {
    "page_schema_version": 1,
    "id": "main",
    "title": "分享看板",
    "layout": "stack",
    "data": {
        "runs": {"source": "run.list", "limit": 20},
        "run": {"source": "run.get", "params": {"id": "$selection.id"}},
    },
    "blocks": [
        {
            "type": "table",
            "title": "运行记录",
            "source": "runs",
            "columns": [
                {"field": "id", "label": "ID", "format": "text"},
                {"field": "spent_usd", "label": "花费", "format": "number"},
            ],
            "onRowClick": {"source": "run", "param": "id"},
        },
        {"type": "detail", "title": "详情", "source": "run"},
        {"type": "button", "label": "取消本次运行", "action": {"action": "run.cancel"}},
    ],
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _package(tmp_path: Path) -> tuple[str, str]:
    workflow_id = new_id("wf_share")
    root = tmp_path / f"pkg_{workflow_id}"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "graph.py").write_text("from wf_sdk import graph, node\n", encoding="utf-8")
    (root / "ui" / "pages" / "main.json").write_text(
        json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8"
    )
    compiler.write_page(SCHEMA, root / "ui" / "compiled" / "main")
    manifest = {"id": workflow_id, "version": 1, "nodes": [], "edges": []}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    manifest_hash, _ = freeze_package(root)
    ref = store_publish(root, manifest, manifest_hash)
    ensure_workflow(workflow_id, tenant_id="default", name="分享测试")
    publish_version(
        workflow_id, version=1, manifest_hash=manifest_hash, package_ref=ref
    )
    return workflow_id, ref


def _link(workflow_id: str, **kwargs: Any) -> str:
    _, token, _ = share_store.create(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        created_by="u_001",
        **kwargs,
    )
    return token


def test_share_is_readable_without_login(
    client: TestClient, tmp_path: Path
) -> None:
    workflow_id, ref = _package(tmp_path)
    create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref=ref,
        subject={"user_id": "u_1"},
        window="share-1",
    )
    token = _link(workflow_id)

    shell = client.get(f"/s/{token}")
    assert shell.status_code == 200
    assert 'sandbox="allow-scripts"' in shell.text
    assert client.get(f"/s/{token}/index.html").status_code == 200

    rows = client.get(f"/s/{token}/data/runs").json()["rows"]
    assert len(rows) == 1 and rows[0]["workflow_id"] == workflow_id


def test_scope_excludes_other_workflows_and_objects(
    client: TestClient, tmp_path: Path
) -> None:
    workflow_id, ref = _package(tmp_path)
    other_id, other_ref = _package(tmp_path)
    create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref=ref,
        subject={"user_id": "u_1"},
        window="share-in",
    )
    foreign_run, _ = create_run(
        tenant_id="default",
        workflow_id=other_id,
        version=1,
        package_ref=other_ref,
        subject={"user_id": "u_1"},
        window="share-out",
    )
    token = _link(workflow_id)

    rows = client.get(f"/s/{token}/data/runs").json()["rows"]
    assert {row["workflow_id"] for row in rows} == {workflow_id}

    # 范围外的单个对象按"不存在"处理，不给"存在但无权"的信号
    assert (
        client.get(f"/s/{token}/data/run?id={foreign_run}").status_code == 404
    )


def test_share_cannot_write(client: TestClient, tmp_path: Path) -> None:
    workflow_id, _ = _package(tmp_path)
    token = _link(workflow_id)
    resp = client.post(f"/s/{token}/action", json={"action": "run.trigger"})
    assert resp.status_code == 403
    assert "只读" in resp.json()["detail"]


def test_revoked_and_expired_links_are_404(
    client: TestClient, tmp_path: Path
) -> None:
    workflow_id, _ = _package(tmp_path)
    share_id, token, _ = share_store.create(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        created_by="u_001",
    )
    assert share_store.revoke(share_id)
    assert client.get(f"/s/{token}").status_code == 404

    revoked_token = _link(workflow_id)
    with tx() as conn:
        conn.execute(
            "UPDATE share_links SET expires_at = now() - interval '1 hour'"
            " WHERE token_hash = encode(sha256(%s::bytea), 'hex')",
            (revoked_token,),
        )
    assert client.get(f"/s/{revoked_token}").status_code == 404
    assert client.get("/s/s_not-a-real-token").status_code == 404


def test_shared_page_hides_buttons_and_cost(
    client: TestClient, tmp_path: Path
) -> None:
    """分享页不出现写按钮，也不暴露成本字段；发布物本身保持原样。"""
    workflow_id, ref = _package(tmp_path)
    token = _link(workflow_id)

    # 页面真值是编进 app.js 的 PAGE 常量，所以投影要看 app.js
    js = client.get(f"/s/{token}/app.js").text
    assert "取消本次运行" not in js
    assert "run.cancel" not in js
    assert "spent_usd" not in js
    assert "花费" not in js

    original = (Path(ref) / "ui" / "compiled" / "main" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "run.cancel" in original and "spent_usd" in original


def test_max_uses_exhausts_with_429(client: TestClient, tmp_path: Path) -> None:
    workflow_id, _ = _package(tmp_path)
    token = _link(workflow_id, max_uses=1)
    assert client.get(f"/s/{token}").status_code == 200
    assert client.get(f"/s/{token}/index.html").status_code == 429
