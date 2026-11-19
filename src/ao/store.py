"""包仓库：内容寻址 + 原子写入。

`packages/<workflow>/<version>-<hash12>/`，写入先落临时目录再 rename；
同一 hash 已存在则直接复用（发布幂等）。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ao import package as package_store
from ao.package import PackageError, freeze_package, load_package
from ao.settings import settings

IGNORE = shutil.ignore_patterns(
    "workspace", "artifacts", "__pycache__", "*.pyc", ".git", ".pytest_cache"
)


def target_dir(workflow_id: str, version: int, manifest_hash: str) -> Path:
    return settings.packages_dir / workflow_id / f"{version}-{manifest_hash[:12]}"


def _object_key(workflow_id: str, version: int, manifest_hash: str) -> str:
    """对象存储里的 key：`<prefix>/<workflow>/<version>-<hash12>`，与本地目录同名。"""
    relative = f"{workflow_id}/{version}-{manifest_hash[:12]}"
    prefix = (settings.package_s3_prefix or "").strip("/")
    return f"{prefix}/{relative}" if prefix else relative


def _upload(target: Path, workflow_id: str, version: int, manifest_hash: str) -> str:
    """把冻结目录整体上传，返回 `s3://bucket/key`。

    先校验后端配置再动手：`s3` 模式却没给 bucket 时必须在发布期失败，
    否则会写出一条指向空位置的 `package_ref`，运行期才炸。
    """
    bucket = settings.package_s3_bucket
    if not bucket:
        raise PackageError("AO_PACKAGE_BACKEND=s3 需要配置 AO_PACKAGE_S3_BUCKET")
    key = _object_key(workflow_id, version, manifest_hash)
    client = package_store._s3_client()
    for path in sorted(target.rglob("*")):
        if path.is_file():
            relative = path.relative_to(target).as_posix()
            client.upload_file(str(path), bucket, f"{key}/{relative}")
    return f"s3://{bucket}/{key}"


def publish(root: Path, manifest: dict[str, Any], manifest_hash: str) -> str:
    workflow_id = str(manifest["id"])
    version = int(manifest["version"])
    target = target_dir(workflow_id, version, manifest_hash)
    if target.exists():
        # 已存在：校验内容一致，防止 hash 相同但内容漂移
        load_package(target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".tmp-{uuid.uuid4().hex[:8]}"
        shutil.copytree(root, tmp, ignore=IGNORE)
        try:
            os.replace(tmp, target)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    if settings.package_backend == "s3":
        return _upload(target, workflow_id, version, manifest_hash)
    return str(target)


def freeze_and_publish(root: Path) -> tuple[str, str]:
    manifest_hash, _ = freeze_package(root)
    package = load_package(root)
    ref = publish(root, package.manifest, manifest_hash)
    return manifest_hash, ref


def verify(ref: str) -> None:
    try:
        load_package(ref)
    except PackageError:
        raise
