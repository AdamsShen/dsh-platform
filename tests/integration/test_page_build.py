"""W43：发布期编译——有 Schema 必须编译成功，坏 Schema 阻断发布。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ao.publish import publish
from ao.settings import PROJECT_ROOT, settings
from tests.unit.test_page_schema import board

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "daily_schedule"
    shutil.copytree(
        EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__", "workspace", "compiled")
    )
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    # 用例会往包里加 ui/，示例自带的 manifest_hash 已失效：去掉让发布流程重新冻结
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_hash", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


@pytest.mark.integration
async def test_publish_compiles_pages_into_package(workspace: Path) -> None:
    pages = workspace / "ui" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "main.json").write_text(json.dumps(board(), ensure_ascii=False), encoding="utf-8")

    result = await publish(workspace)
    assert result.ok, result.as_dict()
    root = Path(result.package_ref)
    assert (root / "ui" / "compiled" / "main" / "index.html").exists()
    assert (root / "ui" / "compiled" / "main" / "app.js").exists()

    # 再发布一次：编译确定性 + 内容寻址，ref 不变
    again = await publish(workspace)
    assert again.ok and again.package_ref == result.package_ref


@pytest.mark.integration
async def test_publish_rejects_invalid_page_schema(workspace: Path) -> None:
    bad = board()
    bad["blocks"][1]["onclick"] = "alert(1)"
    pages = workspace / "ui" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "main.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

    result = await publish(workspace)
    assert not result.ok
    assert result.stage == "pages"
    assert {issue["code"] for issue in result.issues} >= {"E_COMPONENT", "E_EXPR"}


@pytest.mark.integration
async def test_legacy_placeholder_page_is_skipped(workspace: Path) -> None:
    pages = workspace / "ui" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "main.json").write_text(
        json.dumps({"page": "main", "nodes": ["draft"]}), encoding="utf-8"
    )
    # 示例包自带编译产物，这里要模拟"只有占位、从未编译"的旧包
    shutil.rmtree(workspace / "ui" / "compiled", ignore_errors=True)
    result = await publish(workspace)
    assert result.ok, result.as_dict()
    assert not (Path(result.package_ref) / "ui" / "compiled").exists()
