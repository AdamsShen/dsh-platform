"""harness provider 抽象。

对外只有一个方法：`send()` 跑一轮 turn，返回 `Reply`。
provider 不碰数据库、不碰状态机；一切副作用留在执行器里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ao.util import utc_now


@dataclass
class ProbeStatus:
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


class HarnessSession(BaseModel):
    """一次 provider 侧会话（一个 `(run, node, attempt)` 对应一条）。"""

    id: str = Field(default_factory=lambda: f"hs_{utc_now().strftime('%Y%m%d%H%M%S%f')}")
    provider: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    native_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class HarnessReply(BaseModel):
    """一轮 turn 的结果；`status`：ok | fail | needs_permission | needs_auth。"""

    status: str = "ok"
    text: str = ""
    session_id: str = ""
    native_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Reply:
    """执行器眼里的回复；usage 只用于 mock 与无网关时的兜底，账单仍以网关为准。"""

    status: str = "ok"
    text: str = ""
    native_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class Provider(Protocol):
    name: str

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
    ) -> Reply: ...
