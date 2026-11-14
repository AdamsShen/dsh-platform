"""沙箱：bwrap / nsjail 启动参数构造 + 可用性判定。

原则：**fail closed**。后端不可用就拒绝执行并告警，绝不自动回退到
`danger-full-access`（设计 §11）。`AO_SANDBOX=none` 只在 `AO_ENV=dev` 下允许，
且该分支会在节点 meta 里留 `sandbox_fallback=true`。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
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
    #: 是否在沙箱内挂 /proc 与 /dev。实测（研究文档 §4）非特权容器最先死在这两个挂载点上，
    #: 受限环境可关掉；关闭后 harness 若依赖 /proc 需自行承担
    mount_proc: bool = True
    mount_dev: bool = True


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
        mount_proc=settings.sandbox_mount_proc,
        mount_dev=settings.sandbox_mount_dev,
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


def _uid_map_isolated(text: str) -> bool:
    """判断沙箱内 `uid_map` 是否为**非 identity** 映射（即 user namespace 真的建起来了）。

    identity 形如 `0 0 4294967295`：namespace 内外的 uid 一一对应，说明 `--unshare-user-try`
    被跳过，没有获得 userns 内的 CAP_SYS_ADMIN。这种状态下 mount 隔离要么没生效，要么依赖
    宿主 root 权限，对我们"非 root 跑 agent"的前提不成立。
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        inner, outer, length = parts
        if inner == "0" and outer == "0" and length.startswith("4294967295"):
            return False
    return bool(text.strip())


#: 深度探测结果缓存：(mode) -> (monotonic 时间, ok, reason)；避免每次 run 都 fork 一次沙箱
_PROBE_CACHE: dict[str, tuple[float, bool, str]] = {}
_PROBE_TTL_SECONDS = 60.0


def probe(
    mode: str | None = None, *, timeout: float = 15.0, use_cache: bool = True
) -> tuple[bool, str]:
    """实际起一次最小沙箱做隔离断言，返回 `(ok, reason)`。

    为什么不能只查 `shutil.which`：Docker 默认 seccomp 会拦 `clone`/`unshare`，而
    `bwrap --unshare-all` 展开后 user/cgroup 都是 `-try`（失败静默跳过），所以二进制存在
    完全可能"能启动但没隔离"。这里用 uid_map 断言 userns 是否真的生效。
    """
    mode = mode or settings.sandbox
    if mode == "none":
        if settings.env != "dev":
            return False, "AO_SANDBOX=none 只允许 AO_ENV=dev"
        return True, "none（dev 放行，无隔离）"

    if use_cache:
        cached = _PROBE_CACHE.get(mode)
        if cached is not None and time.monotonic() - cached[0] < _PROBE_TTL_SECONDS:
            return cached[1], cached[2]

    try:
        ensure_available(mode)
    except SandboxUnavailable as exc:
        return _remember(mode, False, str(exc), use_cache)

    workdir = Path(tempfile.mkdtemp(prefix="ao-sandbox-probe-"))
    # 探测固定挂 /proc：要读 uid_map 才能断言 userns，与运行时是否挂载无关
    spec = SandboxSpec(
        mode=mode,
        capability="read_only",
        writable=[workdir],
        readonly=[],
        mount_proc=True,
        mount_dev=True,
    )
    argv = launch_argv(spec, ["/bin/sh", "-c", "cat /proc/self/uid_map"])
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        return _remember(mode, False, f"沙箱二进制缺失: {exc}", use_cache)
    except subprocess.TimeoutExpired:
        return _remember(mode, False, f"沙箱探测超时（{timeout}s）", use_cache)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[0] if detail else f"退出码 {completed.returncode}"
        return _remember(mode, False, f"沙箱启动失败: {reason}", use_cache)
    if not _uid_map_isolated(completed.stdout):
        return _remember(
            mode,
            False,
            "user namespace 未生效（uid_map 为 identity）：容器可能未开放非特权 userns，"
            "或 seccomp 拦了 clone/unshare",
            use_cache,
        )
    return _remember(mode, True, "ok", use_cache)


def _remember(mode: str, ok: bool, reason: str, use_cache: bool) -> tuple[bool, str]:
    if use_cache:
        _PROBE_CACHE[mode] = (time.monotonic(), ok, reason)
    return ok, reason


def reset_probe_cache() -> None:
    _PROBE_CACHE.clear()


def require_available(mode: str | None = None) -> None:
    """真跑前的守卫：二进制存在 **且** 隔离断言通过，否则 fail closed。"""
    mode = mode or settings.sandbox
    ensure_available(mode)
    ok, reason = probe(mode)
    if not ok:
        raise SandboxUnavailable(f"沙箱隔离不可用: {reason}")


def build_bwrap(spec: SandboxSpec, command: list[str]) -> list[str]:
    """构造 bwrap 命令行。

    注意 `--unshare-all` 并不强制隔离 user/cgroup：按 bwrap(1) 手册它等价于
    `--unshare-user-try --unshare-ipc --unshare-pid --unshare-net
    --unshare-uts --unshare-cgroup-try`，
    user namespace 建不起来时会**静默跳过**，失败点后移到 mount（非 root 无 CAP_SYS_ADMIN）。
    需要强隔离时应显式写 `--unshare-user` 让它在不可用时直接失败，这一点由 `probe()` 负责断言。
    """
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if spec.mount_proc:
        args += ["--proc", "/proc"]
    if spec.mount_dev:
        args += ["--dev", "/dev"]
    args += ["--tmpfs", "/tmp"]
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
