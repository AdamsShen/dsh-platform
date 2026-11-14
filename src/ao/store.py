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

from ao.package import PackageError, freeze_package, load_package
from ao.settings import settings

IGNORE = shutil.ignore_patterns(
    "workspace", "artifacts", "__pycache__", "*.pyc", ".git", ".pytest_cache"
)


def target_dir(workflow_id: str, version: int, manifest_hash: str) -> Path:
    return settings.packages_dir / workflow_id / f"{version}-{manifest_hash[:12]}"


def publish(root: Path, manifest: dict[str, Any], manifest_hash: str) -> str:
    target = target_dir(str(manifest["id"]), int(manifest["version"]), manifest_hash)
    if target.exists():
        # 已存在：校验内容一致，防止 hash 相同但内容漂移
        load_package(target)
        return str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{uuid.uuid4().hex[:8]}"
    shutil.copytree(root, tmp, ignore=IGNORE)
    try:
        os.replace(tmp, target)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
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
