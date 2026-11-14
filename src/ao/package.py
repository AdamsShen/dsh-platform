"""工作流包：目录化、内容寻址、不可变。

包结构见设计 §7.2。`manifest_hash` 由 manifest + 包内每个文件的 hash 共同决定，
加载时校验，防止"跑的不是发布的"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ao.settings import settings
from ao.util import canonical_json

REQUIRED_FILES = ("manifest.json", "graph.py")


class PackageError(RuntimeError):
    pass


@dataclass
class Package:
    root: Path
    manifest: dict[str, Any]
    manifest_hash: str
    files: dict[str, str] = field(default_factory=dict)

    @property
    def graph_path(self) -> Path:
        return self.root / "graph.py"

    def path(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if self.root.resolve() not in target.parents and target != self.root.resolve():
            raise PackageError(f"包内路径越界: {relative}")
        return target


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_hashes(root: Path) -> tuple[str, dict[str, str]]:
    """manifest_hash = sha256(manifest 规范序列化 + 排序后的逐文件 hash)。"""
    for name in REQUIRED_FILES:
        if not (root / name).exists():
            raise PackageError(f"包缺少 {name}: {root}")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # manifest_hash 本身不参与计算，否则写回 hash 会改变 hash（自引用）
    manifest_for_hash = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        # manifest.json 已由上面的规范序列化覆盖；再算文件 hash 会让"写回 hash"改变 hash
        if relative == "manifest.json":
            continue
        # 运行期产物与导入缓存不属于包内容，否则"跑一次 hash 就变"
        parts = set(Path(relative).parts)
        if parts & {"__pycache__", "workspace", "artifacts"} or relative.startswith("."):
            continue
        if relative.endswith((".pyc", ".pyo")):
            continue
        files[relative] = _file_hash(path)
    material = canonical_json({"manifest": manifest_for_hash, "files": files})
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), files


def resolve_ref(ref: str, packages_dir: Path | None = None) -> Path:
    """支持三种写法：绝对目录、`<id>@<version>`、`<id>/<version>-<hash>`。"""
    candidate = Path(ref)
    if candidate.is_dir():
        return candidate.resolve()

    base = packages_dir or settings.packages_dir
    if "@" in ref:
        workflow_id, _, version = ref.partition("@")
        matches = sorted((base / workflow_id).glob(f"{version}-*"))
        if matches:
            return matches[-1].resolve()
        direct = base / workflow_id / version
        if direct.is_dir():
            return direct.resolve()
    direct = base / ref
    if direct.is_dir():
        return direct.resolve()
    raise PackageError(f"找不到工作流包: {ref}（根目录 {base}）")


def load_package(ref: str | Path, *, verify: bool = True) -> Package:
    root = Path(ref).resolve() if isinstance(ref, Path) else resolve_ref(ref)
    for name in REQUIRED_FILES:
        if not (root / name).exists():
            raise PackageError(f"包缺少 {name}: {root}")

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest_hash, files = compute_hashes(root)

    recorded = manifest.get("manifest_hash")
    if verify and recorded and recorded != manifest_hash:
        raise PackageError(
            f"包内容与 manifest_hash 不一致: 记录 {recorded[:12]}，实际 {manifest_hash[:12]}"
        )
    return Package(root=root, manifest=manifest, manifest_hash=manifest_hash, files=files)


def freeze_package(root: Path) -> tuple[str, dict[str, str]]:
    """发布时调用：算出 hash 并写回 manifest.json（其他字段不动）。"""
    manifest_hash, files = compute_hashes(root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_hash") != manifest_hash:
        manifest["manifest_hash"] = manifest_hash
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest_hash, files = compute_hashes(root)
    return manifest_hash, files
