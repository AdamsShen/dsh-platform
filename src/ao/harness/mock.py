"""确定性 mock provider：不联网、不调模型，用来验证整条链路与失败路径。

不是"降级"：`AO_MOCK=0` 时执行器不会选它；选它的时候 UI 与事件里都明确标记。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ao.harness.base import Reply

INT_FIELD_DEFAULTS = {"conflict_count": 0, "steps": 1}


def _value_for(name: str, kind: str) -> Any:
    if kind == "str":
        return f"mock-{name}"
    if kind == "int":
        return INT_FIELD_DEFAULTS.get(name, 0)
    if kind == "bool":
        return True
    if kind == "float":
        return 0.0
    return None


class MockProvider:
    name = "mock"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.fail_nodes = {
            item.strip() for item in os.environ.get("AO_FAIL_NODE", "").split(",") if item.strip()
        }
        self.conflict = int(os.environ.get("AO_MOCK_CONFLICT", "0") or 0)
        self.calls: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        system: str,
        prompt: str,
        contract: dict[str, Any] | None,
        repair: bool = False,
        native_id: str | None = None,
        context: dict[str, Any] | None = None,
        on_activity: Any = None,
    ) -> Reply:
        await asyncio.sleep(self.delay)
        self.calls.append(
            {"run_id": run_id, "node_id": node_id, "attempt": attempt, "repair": repair}
        )

        if node_id in self.fail_nodes:
            # 故意返回不可解析的文本，验证"产物不合格 → repair → 仍不合格 → 节点失败"
            return Reply(
                status="ok",
                text="这个回复没有 result 块",
                native_id=f"mock-{node_id}-{attempt}",
                usage={"input_tokens": 100, "output_tokens": 20, "model": "mock-1"},
            )

        outputs: dict[str, str] = dict((contract or {}).get("required") or {})
        payload = {name: _value_for(name, kind) for name, kind in outputs.items()}
        if "conflict_count" in payload and self.conflict:
            payload["conflict_count"] = self.conflict

        text = (
            f"[mock] node={node_id} attempt={attempt} repair={repair}\n"
            "```result\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n"
            "```"
        )
        return Reply(
            status="ok",
            text=text,
            native_id=f"mock-{node_id}-{attempt}",
            usage={
                "input_tokens": 120 + len(prompt) // 4,
                "output_tokens": 40,
                "model": "mock-1",
            },
            tool_calls=[{"tool": f"mcp__mock__{node_id}", "ok": True}],
        )


async def probe() -> ProbeStatusLike:
    return ProbeStatusLike(status="ok", detail="mock provider 始终可用")


class ProbeStatusLike:
    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail
