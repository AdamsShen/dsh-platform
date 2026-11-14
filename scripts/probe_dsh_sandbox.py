"""DSH 自带沙箱实测：验证 permission mode 是否真的生效。

判定方式不依赖模型自述，而是看**真实文件系统副作用**：
- 正向：workspace-write 下写 cwd 内文件，跑完文件必须存在；
- 负向：写 `$HOME` 下文件，跑完文件必须**不存在**（普通用户对 HOME 有写权限，
  若写成功就说明沙箱没生效）；
- read-only 下写 cwd 也必须被拒。

用法：
    uv run python scripts/probe_dsh_sandbox.py
    uv run python scripts/probe_dsh_sandbox.py --case home   # 只跑某一组
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ESCAPE = ".dsh_sbx_escape_probe"


def _settings() -> Any:
    from ao.settings import settings

    return settings


def _run_case(mode: str, command: str, cwd: Path, timeout: float = 120.0) -> dict[str, Any]:
    """按指定档位跑一轮，返回结果与工具调用摘要（不回写任何任务目录）。"""
    from deepseek_harness import DeepSeekHarness

    s = _settings()
    home = Path(tempfile.mkdtemp(prefix="dsh-sbx-home-"))
    env = {
        "DSH_PERMISSION_MODE": mode,
        "DSH_AGENTS_HOME": str(home / "agents"),
    }
    api_key = str(getattr(s, "dsh_api_key", "") or os.environ.get("DEEPSEEK_API_KEY", ""))
    kwargs: dict[str, Any] = {
        "provider": str(getattr(s, "dsh_llm_provider", "") or "deepseek-official"),
        "model": str(getattr(s, "dsh_model", "") or "deepseek-v4-flash"),
        "cwd": str(cwd),
        "profile": str(getattr(s, "dsh_profile", "") or "sdk"),
        "patches": (),
        "dsh_home": str(home),
        "api_key": api_key or None,
        "request_timeout_seconds": timeout,
        "env": env,
    }
    prompt = (
        "这是一次沙箱能力验证，命令本身无副作用。请只调用一次 bash 工具执行下面这条命令，"
        "不要修改命令、不要解释、执行完只回答 done：\n\n"
        f"`{command}`"
    )
    harness: Any = None
    started = time.perf_counter()
    try:
        harness = DeepSeekHarness(**kwargs)
        session = harness.start_session(f"sbx-{uuid.uuid4().hex[:8]}")
        result = session.run(prompt)
        events = list(getattr(result, "events", None) or [])
        calls: list[str] = []
        tool_errors: list[str] = []
        tool_outputs: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "tool/call":
                data = event.get("data") or {}
                calls.append(str(data.get("name") or ""))
            elif event.get("type") == "tool/result":
                message = (event.get("data") or {}).get("message") or {}
                blob = json.dumps(message, ensure_ascii=False, default=str)[:400]
                tool_outputs.append(blob)
                if message.get("isError"):
                    tool_errors.append(blob)
        return {
            "mode": mode,
            "command": command,
            "elapsed": round(time.perf_counter() - started, 2),
            "status": str(getattr(result, "finish_reason", "") or ""),
            "text": str(getattr(result, "final_response", "") or "")[:200],
            "tools": calls,
            "tool_errors": tool_errors,
            "tool_outputs": tool_outputs,
            "cwd": str(cwd),
        }
    finally:
        if harness is not None:
            try:
                harness.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="all", choices=["all", "basic", "inside", "home", "readonly"])
    args = parser.parse_args()

    s = _settings()
    print(f"[env] permission_mode={getattr(s, 'dsh_permission_mode', '')!r} "
          f"auto_fallback={getattr(s, 'dsh_sandbox_auto_fallback', None)}")
    print(f"[env] api_key={'set' if getattr(s, 'dsh_api_key', '') else 'MISSING'} "
          f"model={getattr(s, 'dsh_model', '')!r}")

    escape_target = Path(os.path.expanduser("~")) / ESCAPE
    if escape_target.exists():
        escape_target.unlink()

    cases: list[tuple[str, str, str]] = [
        ("basic", "workspace-write", "echo __sbx_basic_ok__"),
        ("inside", "workspace-write", "echo inside > ./inside.txt && cat ./inside.txt"),
        ("home", "workspace-write", f"touch ~/{ESCAPE}"),
        ("readonly", "read-only", "echo ro > ./ro.txt"),
    ]
    selected = [c for c in cases if args.case in {"all", c[0]}]

    results: list[dict[str, Any]] = []
    workdirs: list[Path] = []
    try:
        for name, mode, command in selected:
            cwd = Path(tempfile.mkdtemp(prefix=f"dsh-sbx-{name}-"))
            workdirs.append(cwd)
            print(f"\n[{name}] mode={mode} cmd={command}")
            info = _run_case(mode, command, cwd)
            info["case"] = name
            info["inside_exists"] = (cwd / "inside.txt").exists()
            info["ro_exists"] = (cwd / "ro.txt").exists()
            info["escape_exists"] = escape_target.exists()
            results.append(info)
            print(f"  status={info['status']} elapsed={info['elapsed']}s tools={info['tools']}")
            print(f"  inside={info['inside_exists']} ro={info['ro_exists']} escape={info['escape_exists']}")
            if info["tool_errors"]:
                print(f"  tool_error: {info['tool_errors'][0][:200]}")
            for out in info["tool_outputs"][:2]:
                print(f"  tool_result: {out[:260]}")
            print(f"  text: {info['text'][:120]!r}")
    finally:
        for d in workdirs:
            shutil.rmtree(d, ignore_errors=True)
        if escape_target.exists():
            escape_target.unlink()

    def _denied(info: dict[str, Any]) -> bool:
        """拒绝必须是沙箱给出的，而不是命令静默失败或模型没执行。"""
        blob = (" ".join(info["tool_outputs"]) + " " + info["text"]).lower()
        return bool(info["tool_errors"]) or any(
            hint in blob for hint in ("denied", "not permitted", "permission", "not allowed")
        )

    by = {r["case"]: r for r in results}
    checks: list[tuple[str, bool, str]] = []
    if "basic" in by:
        checks.append(
            ("workspace-write 下 bash 可执行", "bash" in by["basic"]["tools"],
             f"tools={by['basic']['tools']}")
        )
    if "inside" in by:
        checks.append(("workspace-write 允许写 cwd", by["inside"]["inside_exists"],
                       f"inside.txt={by['inside']['inside_exists']}"))
    if "home" in by:
        ok = (not by["home"]["escape_exists"]) and _denied(by["home"])
        checks.append(("workspace-write 拒绝写 HOME 越界", ok,
                       f"存在={by['home']['escape_exists']} denied={_denied(by['home'])}"))
    if "readonly" in by:
        ok = (not by["readonly"]["ro_exists"]) and _denied(by["readonly"])
        checks.append(("read-only 拒绝写 cwd", ok,
                       f"存在={by['readonly']['ro_exists']} denied={_denied(by['readonly'])}"))

    print("\n== 判定 ==")
    bad = 0
    for label, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}（{detail}）")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
