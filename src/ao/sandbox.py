"""沙箱：bwrap / nsjail 启动参数构造 + 可用性判定。

原则：**fail closed**。后端不可用就拒绝执行并告警，绝不自动回退到
`danger-full-access`（设计 §11）。`AO_SANDBOX=none` 只在 `AO_ENV=dev` 下允许，
且该分支会在节点 meta 里留 `sandbox_fallback=true`。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ao.settings import settings


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class SandboxSpec:
    mode: str
    capability: str
    writable: list[Path]
    readonly: list[Path]


#: 能力包 → 可写目录；`full_access` 平台侧不开放
CAPABILITIES = {
    "read_only": [],
    "workspace_write": ["workspace", "artifacts"],
}

#: harness 启动时会自动加载的文件，必须在 input/ 里清掉
AUTOLOAD_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", ".mcp.json")


def resolve(capability: str, package_root: Path) -> SandboxSpec:
    if capability not in CAPABILITIES:
        raise SandboxUnavailable(f"能力包未开放: {capability}（danger-full-access 平台禁用）")
    writable = [package_root / name for name in CAPABILITIES[capability]]
    return SandboxSpec(
        mode=settings.sandbox,
        capability=capability,
        writable=writable,
        readonly=[package_root / "prompts", package_root / "contracts"],
    )


def ensure_available(mode: str | None = None) -> None:
    mode = mode or settings.sandbox
    if mode == "none":
        if settings.env != "dev":
            raise SandboxUnavailable("AO_SANDBOX=none 只允许 AO_ENV=dev，生产必须启用隔离")
        return
    binary = {"bwrap": "bwrap", "nsjail": "nsjail"}.get(mode)
    if binary is None:
        raise SandboxUnavailable(f"未知沙箱后端: {mode}")
    if shutil.which(binary) is None:
        raise SandboxUnavailable(f"沙箱后端未安装: {binary}")


def build_bwrap(spec: SandboxSpec, command: list[str]) -> list[str]:
    """构造 bwrap 命令行；`--unshare-all` 一并隔离 net/pid/user/ipc/uts。"""
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    for path in spec.readonly:
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for path in spec.writable:
        path.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(path), str(path)]
    args += ["--chdir", str(spec.writable[0] if spec.writable else Path.cwd())]
    return args + command


def build_nsjail(spec: SandboxSpec, command: list[str]) -> list[str]:
    chroot = "/" if spec.writable else "/"
    args = [
        "nsjail",
        "--quiet",
        "--mode",
        "l",
        "--chroot",
        chroot,
        "--disable_clone_newcgroup",
        "--time_limit",
        "600",
    ]
    if spec.mode == "nsjail":
        args += ["--disable_clone_newnet"]
    for path in spec.writable:
        path.mkdir(parents=True, exist_ok=True)
        args += ["--bindmount", f"{path}:{path}"]
    return args + ["--"] + command


def launch_argv(spec: SandboxSpec, command: list[str]) -> list[str]:
    ensure_available(spec.mode)
    if spec.mode == "bwrap":
        return build_bwrap(spec, command)
    if spec.mode == "nsjail":
        return build_nsjail(spec, command)
    return command


def sanitize_inputs(inputs: Path) -> list[str]:
    """删除 harness 会自动加载的文件；删不掉就报错（fail closed）。"""
    removed: list[str] = []
    if not inputs.exists():
        return removed
    for name in AUTOLOAD_FILES:
        target = inputs / name
        if not target.exists() and not target.is_symlink():
            continue
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            removed.append(name)
        except OSError as exc:
            raise SandboxUnavailable(f"输入中和失败，拒绝执行: {name}: {exc}") from exc
    return removed
