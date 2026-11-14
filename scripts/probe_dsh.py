"""真实 DSH 单轮探针：验证 SDK、鉴权、契约输出与耗时。

这一步对应 P1-T02 的验收：不是"能 import"，而是"真的跑完一轮并拿到契约"。
用法：
    DEEPSEEK_API_KEY=... uv run python scripts/probe_dsh.py
可选：
    AO_DSH_MODEL=deepseek-chat   指定模型
    AO_DSH_SANDBOX=read_only|workspace-write
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from ao.harness.dsh import DshProvider

CONTRACT = {
    "type": "object",
    "required": ["ok", "answer"],
    "properties": {"ok": {"type": "boolean"}, "answer": {"type": "string"}},
}

PROMPT = (
    "这是一个连通性探针。请只输出一个 ```result 代码块，内容为 "
    '{"ok": true, "answer": "pong"}，不要调用任何工具，不要输出其它文字。'
)

_RESULT_RE = re.compile(r"```result\s*(\{.*?\})\s*```", re.DOTALL)


async def main() -> int:
    provider = DshProvider()
    status = provider.probe()
    print(f"[probe] status={status.status} detail={status.detail}")
    if status.status != "ok":
        return 2

    cwd = Path(tempfile.mkdtemp(prefix="ao-probe-"))
    capability = "read_only"
    activities: list[dict] = []
    started = time.perf_counter()
    reply = await provider.send(
        run_id="probe_run",
        node_id="probe_node",
        attempt=0,
        system="你是一个只负责按要求输出 JSON 的执行节点。",
        prompt=PROMPT,
        contract=CONTRACT,
        context={
            "cwd": str(cwd),
            "capability": capability,
            "timeout": 180,
        },
        on_activity=activities.append,
    )
    elapsed = time.perf_counter() - started

    match = _RESULT_RE.search(reply.text or "")
    parsed: dict | None = None
    parse_error = ""
    if match:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            parse_error = str(exc)

    print(f"[turn] status={reply.status} elapsed={elapsed:.2f}s native_id={reply.native_id}")
    print(f"[turn] text_len={len(reply.text or '')} activities={len(activities)}")
    print(f"[turn] meta_keys={sorted((reply.meta or {}).keys())}")
    if parsed is not None:
        print(f"[contract] parsed ok fields={sorted(parsed)} answer={parsed.get('answer')!r}")
    else:
        print(f"[contract] 未解析到 result 块 parse_error={parse_error or 'no match'}")
        if reply.text:
            print(f"[contract] 文本开头: {reply.text[:200]!r}")

    # 失败时把 meta 里的错误也带出来，避免只看到 status=fail
    if reply.status != "ok":
        meta = reply.meta or {}
        print(f"[error] type={meta.get('error_type')} message={str(meta.get('error'))[:300]}")
        return 1
    return 0 if parsed is not None else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
