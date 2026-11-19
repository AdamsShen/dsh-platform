"""W43：页面托管——iframe 沙箱、版本路径、短期 token、数据越权。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ao.package import freeze_package
from ao.pages import compiler
from ao.pages import hosting as page_hosting
from ao.runs import create_run, ensure_workflow, publish_version
from ao.store import publish as store_publish
from ao.util import new_id
from ao.web import create_app
from tests.unit.test_page_schema import board


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _package(tmp_path: Path, workflow_id: str, schema: dict[str, Any]) -> str:
    """造一个冻结包：manifest + graph + ui/pages/<page>.json + ui/compiled/<page>/。"""
    root = tmp_path / f"pkg_{workflow_id}"
    (root / "ui" / "pages").mkdir(parents=True)
    (root / "graph.py").write_text("from wf_sdk import graph, node\n", encoding="utf-8")
    (root / "ui" / "pages" / "main.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    compiler.write_page(schema, root / "ui" / "compiled" / "main")
    manifest = {"id": workflow_id, "version": 1, "nodes": [], "edges": []}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    manifest_hash, _ = freeze_package(root)
    ref = store_publish(root, manifest, manifest_hash)
    ensure_workflow(workflow_id, tenant_id="default", name="托管测试")
    publish_version(
        workflow_id, version=1, manifest_hash=manifest_hash, package_ref=ref
    )
    return ref


def _schema() -> dict[str, Any]:
    schema = board()
    schema["data"] = {
        "runs": {"source": "run.list", "limit": 20},
        "run": {"source": "run.get"},
        "events": {"source": "run.events"},
    }
    # 看板里的 sourced 组件引用的是 runs/run/events，保持名字一致即可
    return schema


@pytest.fixture
def hosted(tmp_path: Path) -> tuple[str, str]:
    workflow_id = new_id("wf_host")
    schema = _schema()
    ref = _package(tmp_path, workflow_id, schema)
    return workflow_id, ref


def test_shell_uses_sandbox_without_same_origin(
    client: TestClient, hosted: tuple[str, str]
) -> None:
    workflow_id, _ = hosted
    resp = client.get(f"/p/{workflow_id}/1/main")
    assert resp.status_code == 200
    body = resp.text
    assert 'sandbox="allow-scripts"' in body
    assert "allow-same-origin" not in body
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert 'src="./main/index.html?token=' in body


def test_assets_are_served_with_csp_and_unknown_paths_404(
    client: TestClient, hosted: tuple[str, str]
) -> None:
    workflow_id, _ = hosted
    index = client.get(f"/p/{workflow_id}/1/main/index.html")
    assert index.status_code == 200
    assert "Content-Security-Policy" in index.headers
    assert index.headers["cache-control"] == "no-store"

    js = client.get(f"/p/{workflow_id}/1/main/app.js")
    assert js.status_code == 200
    assert "public, max-age=86400" in js.headers["cache-control"]

    # 只放行三个固定文件与 assets/，包里的 manifest.json 不能顺着路径取到
    assert client.get(f"/p/{workflow_id}/1/main/manifest.json").status_code == 404
    assert client.get(f"/p/{workflow_id}/1/main/../manifest.json").status_code == 404
    assert client.get(f"/p/{workflow_id}/1/nope/index.html").status_code == 404
    assert client.get(f"/p/{workflow_id}/9/main/index.html").status_code == 404


def test_data_endpoint_enforces_token_and_tenant(
    client: TestClient, hosted: tuple[str, str]
) -> None:
    workflow_id, ref = hosted
    create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref=ref,
        subject={"user_id": "u_1"},
        window="host-1",
    )
    token = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        user_id="u_1",
        tenant_id="default",
    )
    ok = client.get(
        f"/api/page/{workflow_id}/1/main/data?source=runs&token={token}"
    )
    assert ok.status_code == 200
    rows = ok.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == workflow_id
    # 响应里不带内部字段（错误堆栈、租约持有者等）
    assert "error" not in rows[0] and "lease_owner" not in rows[0]

    assert (
        client.get(f"/api/page/{workflow_id}/1/main/data?source=runs&token=bad").status_code
        == 401
    )
    expired = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        user_id="u_1",
        tenant_id="default",
        ttl=-1,
    )
    assert (
        client.get(
            f"/api/page/{workflow_id}/1/main/data?source=runs&token={expired}"
        ).status_code
        == 401
    )
    other_page = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="other",
        user_id="u_1",
        tenant_id="default",
    )
    assert (
        client.get(
            f"/api/page/{workflow_id}/1/main/data?source=runs&token={other_page}"
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/api/page/{workflow_id}/1/main/data?source=unknown&token={token}"
        ).status_code
        == 400
    )


def test_run_get_is_scoped_to_tenant(client: TestClient, hosted: tuple[str, str]) -> None:
    workflow_id, ref = hosted
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=workflow_id,
        version=1,
        package_ref=ref,
        subject={"user_id": "u_1"},
        window="host-2",
    )
    foreign = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        user_id="u_2",
        tenant_id="other-tenant",
    )
    resp = client.get(
        f"/api/page/{workflow_id}/1/main/data?source=run&id={run_id}&token={foreign}"
    )
    assert resp.status_code == 404

    own = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        user_id="u_1",
        tenant_id="default",
    )
    assert (
        client.get(
            f"/api/page/{workflow_id}/1/main/data?source=run&id={run_id}&token={own}"
        ).status_code
        == 200
    )


def test_token_rejects_tampered_payload(hosted: tuple[str, str]) -> None:
    workflow_id, _ = hosted
    token = page_hosting.issue_token(
        workflow_id=workflow_id,
        version=1,
        page_id="main",
        user_id="u_1",
        tenant_id="default",
    )
    body, signature = token.split(".", 1)
    with pytest.raises(page_hosting.HostingError):
        page_hosting.verify_token(f"{body}x.{signature}")
    with pytest.raises(page_hosting.HostingError):
        page_hosting.verify_token("no-dot-token")
