from __future__ import annotations

import json
from pathlib import Path

import pytest

from ao.package import PackageError, compute_hashes, freeze_package, load_package


def make_package(tmp_path: Path) -> Path:
    root = tmp_path / "wf"
    (root / "prompts").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"id": "wf", "version": 1, "nodes": [{"id": "a"}]}), encoding="utf-8"
    )
    (root / "graph.py").write_text("G = None\n", encoding="utf-8")
    (root / "prompts" / "a.md").write_text("hello", encoding="utf-8")
    return root


def test_hash_is_stable_after_freeze(tmp_path: Path) -> None:
    root = make_package(tmp_path)
    first, _ = freeze_package(root)
    second, _ = freeze_package(root)
    assert first == second
    package = load_package(root)
    assert package.manifest_hash == first


def test_tamper_is_detected(tmp_path: Path) -> None:
    root = make_package(tmp_path)
    freeze_package(root)
    (root / "graph.py").write_text("G = 1\n", encoding="utf-8")
    with pytest.raises(PackageError, match="manifest_hash 不一致"):
        load_package(root)


def test_missing_required_file(tmp_path: Path) -> None:
    root = make_package(tmp_path)
    # graph.py 已不是必需文件；缺 manifest.json 才是硬错误
    (root / "manifest.json").unlink()
    with pytest.raises(PackageError, match="缺少 manifest.json"):
        compute_hashes(root)
