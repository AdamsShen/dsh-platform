"""W31：对象存储后端——发布上传、运行期按 s3:// 引用下载缓存。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ao import package as package_store
from ao import store
from ao.settings import settings


class FakeS3:
    """最小 S3 替身：只实现 publish/download 用到的三个方法。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, path: str, bucket: str, key: str) -> None:
        self.objects[(bucket, key)] = Path(path).read_bytes()

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
        keys = [
            key
            for (bucket, key) in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": [{"Key": key} for key in sorted(keys)]}

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.objects[(bucket, key)])


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(package_store, "_s3_client", lambda: fake)
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "package_backend", "s3")
    monkeypatch.setattr(settings, "package_s3_bucket", "bucket-1")
    monkeypatch.setattr(settings, "package_s3_prefix", "ao")
    return fake


def _make_package(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "wf_s3",
        "version": 1,
        "manifest_version": 2,
        "nodes": [{"id": "a", "kind": "agent"}],
        "edges": [],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (root / "graph.py").write_text('from wf_sdk import graph, node\n', encoding="utf-8")
    return manifest


def test_publish_uploads_and_resolve_downloads(s3: FakeS3, tmp_path: Path) -> None:
    root = tmp_path / "draft"
    manifest = _make_package(root)
    manifest_hash, _ = package_store.freeze_package(root)

    ref = store.publish(root, manifest, manifest_hash)
    assert ref == f"s3://bucket-1/ao/wf_s3/1-{manifest_hash[:12]}"
    assert any(key.endswith("manifest.json") for _, key in s3.objects)
    assert any(key.endswith("graph.py") for _, key in s3.objects)

    # 换一个空目录，强制走下载（本地内容寻址目录里已经没有这份包）
    (tmp_path / "fresh").mkdir()
    settings.packages_dir = tmp_path / "fresh"
    resolved = package_store.resolve_ref(ref)
    assert (resolved / "manifest.json").exists()
    assert (resolved / "graph.py").read_text(encoding="utf-8").startswith("from wf_sdk")

    # 第二次解析命中缓存，不再访问 S3
    calls = {"count": 0}

    def counting() -> FakeS3:
        calls["count"] += 1
        return s3

    package_store._s3_client = counting
    again = package_store.resolve_ref(ref)
    assert again == resolved
    assert calls["count"] == 0


def test_s3_backend_without_bucket_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "package_backend", "s3")
    monkeypatch.setattr(settings, "package_s3_bucket", "")
    root = tmp_path / "draft"
    manifest = _make_package(root)
    manifest_hash, _ = package_store.freeze_package(root)
    with pytest.raises(package_store.PackageError, match="AO_PACKAGE_S3_BUCKET"):
        store.publish(root, manifest, manifest_hash)


def test_local_backend_returns_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "packages_dir", tmp_path / "packages")
    monkeypatch.setattr(settings, "package_backend", "local")
    root = tmp_path / "draft"
    manifest = _make_package(root)
    manifest_hash, _ = package_store.freeze_package(root)
    ref = store.publish(root, manifest, manifest_hash)
    assert Path(ref).is_dir() and (Path(ref) / "manifest.json").exists()
