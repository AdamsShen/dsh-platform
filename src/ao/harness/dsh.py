"""真实 DSH provider：把移植来的运行时包装成执行器需要的 `send()`。

移植自 `/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/dsh.py`，
只改 import 与配置来源；patch 组装、沙箱探测、resume 逻辑不做改动，便于两边对照。

已知差异（不静默）：
- 一个会话只有一个可写根；
- 没有回合上限配置，长回合靠外层超时 + 运行时看门狗；
- 工具只能整行禁用；
- 事件里没有 token 用量，账单以网关 spend log 为准。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ao.harness.base import HarnessSession, ProbeStatus, Reply

logger = logging.getLogger(__name__)


def sdk_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("deepseek_harness") is not None


class DshProvider:
    name = "dsh"
    supports_extra_write_dirs = False

    def __init__(self) -> None:
        self._runtime: Any = None

    def _get_runtime(self) -> Any:
        if self._runtime is None:
            from ao.harness._dsh_runtime import DshProvider as RuntimeProvider

            self._runtime = RuntimeProvider()
        return self._runtime

    def probe(self) -> ProbeStatus:
        if not sdk_available():
            return ProbeStatus(
                status="missing",
                detail="deepseek-harness-sdk 未安装：uv sync --extra harness",
            )
        from ao.settings import settings

        if not settings.dsh_api_key:
            return ProbeStatus(status="auth_required", detail="DEEPSEEK_API_KEY 未设置")
        return ProbeStatus(
            status="ok",
            detail=f"model={settings.dsh_model or 'default'} profile={settings.dsh_profile}",
        )

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
        on_activity: Callable[[dict[str, Any]], None] | None = None,
    ) -> Reply:
        runtime = self._get_runtime()
        context = context or {}
        session = HarnessSession(
            id=f"hs_{run_id}_{node_id}_{attempt}",
            provider=self.name,
            native_id=native_id or "",
            meta={"run_id": run_id, "node": node_id, "attempt": attempt, "repair": repair},
        )
        # 系统词与用户词合成为一条消息；DSH 若不支持自定义 system 段就落在这里，
        # 执行器会把 system_injected 写进 node.meta（不静默）。
        composed = f"{system}\n\n{prompt}".strip() if system else prompt
        context_pack = {
            "sandbox": {"mode": context.get("capability") or "read_only"},
            "mcp_servers": context.get("mcp_servers") or {},
            "disallowed_tools": context.get("disallowed_tools") or [],
        }
        timeout = float(context.get("timeout") or 600)

        def _notify(event: Any) -> None:
            if on_activity is None:
                return
            try:
                on_activity(event if isinstance(event, dict) else {"raw": str(event)})
            except Exception:  # noqa: BLE001 — 通知失败不能影响 turn
                logger.debug("activity 通知失败", exc_info=True)

        try:
            replied = await asyncio.wait_for(
                runtime.send(
                    session,
                    composed,
                    cwd=str(context.get("cwd") or Path.cwd()),
                    context_pack=context_pack,
                    on_activity=_notify,
                    # 网关模式：执行器按 turn 注入虚拟 key 与网关端点
                    api_key=context.get("api_key"),
                    base_url=context.get("gateway_base_url"),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "DSH 单轮超时: run=%s node=%s attempt=%s timeout=%.0fs",
                run_id,
                node_id,
                attempt,
                timeout,
            )
            return Reply(
                status="fail",
                text="",
                native_id=session.native_id,
                meta={"timeout": True, "system_injected": True},
            )

        return Reply(
            status=replied.status,
            text=replied.text,
            native_id=replied.native_id or session.native_id,
            usage={},
            meta={**replied.meta, "system_injected": True},
        )
