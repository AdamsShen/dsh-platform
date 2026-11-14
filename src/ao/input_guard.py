"""输入中和：清理 harness 会自动加载的指令文件。

harness 启动时会自动读取工作目录下的 ``AGENTS.md`` / ``CLAUDE.md`` / ``.cursorrules``
等文件。如果这些文件能随输入一起进入 ``input/``，就等于把提示词注入的入口交给了
不可信内容，因此在交给 harness 之前统一定点删除；删不掉就拒绝执行（fail closed）。

注意：**本模块不做进程隔离**。执行隔离统一交给 harness 原生的 permission mode
（``read-only`` / ``workspace-write`` / ``danger-full-access``，见
``ao/harness/_dsh_runtime.py``），平台侧不再自己构造 bwrap/nsjail 命令行。
"""

from __future__ import annotations

import shutil
from pathlib import Path


class InputGuardError(RuntimeError):
    pass


#: harness 启动时会自动加载的文件，必须在 input/ 里清掉
AUTOLOAD_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", ".mcp.json")


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
            raise InputGuardError(f"输入中和失败，拒绝执行: {name}: {exc}") from exc
    return removed
