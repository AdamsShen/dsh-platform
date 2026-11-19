from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ao.package import load_package
from ao.publish import publish
from ao.settings import PROJECT_ROOT, settings

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "daily_schedule"
    shutil.copytree(EXAMPLE, root)
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    return root


@pytest.mark.integration
async def test_publish_happy_path(workspace: Path) -> None:
    result = await publish(workspace)
    assert result.ok, result.as_dict()
    assert result.stage == "published"
    assert result.smoke and result.smoke["ok"]
    assert Path(result.package_ref).is_dir()
    # 发布幂等：再次发布复用同一个目录
    again = await publish(workspace)
    assert again.ok and again.package_ref == result.package_ref
    loaded = load_package(result.package_ref)
    assert loaded.manifest["id"] == "daily_schedule"


@pytest.mark.integration
async def test_publish_rejects_unknown_edge(workspace: Path) -> None:
    """声明化后 manifest 就是真值：边指向不存在的节点在解析阶段就该拦住。"""
    path = workspace / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edges"] = [{"from": "draft", "to": "ghost"}]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = await publish(workspace, smoke=False)
    assert not result.ok
    assert result.stage == "manifest"
    assert "E_MANIFEST" in {i["code"] for i in result.issues}


@pytest.mark.integration
async def test_publish_rejects_missing_prompt(workspace: Path) -> None:
    (workspace / "prompts" / "finalize.md").unlink()
    result = await publish(workspace, smoke=False)
    assert not result.ok
    assert result.stage == "files"


@pytest.mark.integration
async def test_publish_rejects_bad_prompt_template(workspace: Path) -> None:
    """可执行代码已不存在，模板就是唯一会"执行"的东西：语法错必须拦在发布前。"""
    prompt = workspace / "prompts" / "finalize.md"
    prompt.write_text("{% if %}\n坏了\n", encoding="utf-8")
    result = await publish(workspace, smoke=False)
    assert not result.ok
    assert result.stage == "static"
    assert "E_PROMPT_SYNTAX" in {i["code"] for i in result.issues}


@pytest.mark.integration
async def test_publish_fails_when_smoke_fails(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AO_FAIL_NODE", "draft")
    result = await publish(workspace)
    assert not result.ok
    assert result.stage == "smoke"
    assert result.issues[0]["code"] == "E_SMOKE"


@pytest.mark.integration
async def test_manifest_hash_is_stable_across_publish(workspace: Path) -> None:
    first = await publish(workspace)
    assert first.ok
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_hash"] == first.manifest_hash
