"""LiteLLM 网关：虚拟 key + spend log。

账单真值只在网关：
- 每次 `(run, node, attempt, purpose)` 前签一个虚拟 key，`attempt` 终态吊销；
- `llm_usage` 定时从 `/spend/logs` 拉取，按 `gateway_request_id` 去重；
- `runs.spent_usd` 由 `llm_usage` 聚合刷新，不做增量累加。

HTTP 路径按 LiteLLM Proxy 的公开接口写；接口若有差异，改这里一处。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ao.settings import settings
from ao.util import utc_now

logger = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"网关返回 {status}: {message}")
        self.status = status


class BudgetExceeded(GatewayError):
    """402：预算耗尽，run 必须进 paused_budget，不能当瞬态失败重试。"""


@dataclass
class VirtualKey:
    key: str
    key_id: str
    expires_at: str | None = None


class Gateway(Protocol):
    async def issue_key(
        self, *, run_id: str, node_id: str, attempt: int, purpose: str, budget_usd: float
    ) -> VirtualKey: ...

    async def revoke_key(self, key: str) -> None: ...

    async def spend_logs(self, *, start: str, end: str) -> list[dict[str, Any]]: ...


class NullGateway:
    """mock 模式：不签 key、不拉流水，调用方按本地价目表估算。"""

    async def issue_key(self, **_: Any) -> VirtualKey:
        return VirtualKey(key="sk-mock", key_id="mock")

    async def revoke_key(self, key: str) -> None:
        return None

    async def spend_logs(self, **_: Any) -> list[dict[str, Any]]:
        return []


class LiteLLMGateway:
    def __init__(
        self,
        base_url: str | None = None,
        master_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or settings.gateway_url).rstrip("/")
        self.master_key = master_key or settings.gateway_master_key
        self.transport = transport
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.master_key}"}
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            transport=self.transport,
        )

    async def issue_key(
        self, *, run_id: str, node_id: str, attempt: int, purpose: str, budget_usd: float
    ) -> VirtualKey:
        payload = {
            "key_alias": f"{run_id}:{node_id}:{attempt}:{purpose}",
            "metadata": {
                "run_id": run_id,
                "node_id": node_id,
                "attempt": attempt,
                "purpose": purpose,
            },
            "max_budget": round(budget_usd, 6) if budget_usd > 0 else None,
            "duration": "1d",
        }
        async with self._client() as client:
            resp = await client.post("/key/generate", json=payload)
        if resp.status_code == 402:
            raise BudgetExceeded(402, resp.text)
        if resp.status_code >= 400:
            raise GatewayError(resp.status_code, resp.text[:300])
        data = resp.json()
        key_id = data.get("key_name") or data.get("token") or ""
        return VirtualKey(key=data.get("key", ""), key_id=str(key_id))

    async def revoke_key(self, key: str) -> None:
        async with self._client() as client:
            resp = await client.post("/key/delete", json={"keys": [key]})
        if resp.status_code >= 400:
            logger.warning("虚拟 key 吊销失败（保留支出即可，不阻塞收尾）: %s", resp.text[:200])

    async def spend_logs(self, *, start: str, end: str) -> list[dict[str, Any]]:
        params = {"start_date": start, "end_date": end}
        async with self._client() as client:
            resp = await client.get("/spend/logs", params=params)
        if resp.status_code >= 400:
            raise GatewayError(resp.status_code, resp.text[:300])
        data = resp.json()
        return data if isinstance(data, list) else []


def current_gateway() -> Gateway:
    if settings.mock:
        return NullGateway()
    if not settings.gateway_url or not settings.gateway_master_key:
        # 不静默降级：真跑必须有网关，否则账单不可信
        raise RuntimeError("AO_MOCK=0 但 AO_GATEWAY_URL / AO_GATEWAY_MASTER_KEY 未配置")
    return LiteLLMGateway()


def normalize_spend(row: dict[str, Any]) -> dict[str, Any]:
    """把网关流水映射成 `llm_usage` 需要的形状。"""
    metadata = row.get("metadata") or {}
    return {
        "run_id": metadata.get("run_id") or row.get("run_id"),
        "node_id": metadata.get("node_id"),
        "attempt": int(metadata.get("attempt") or 0),
        "purpose": metadata.get("purpose") or "node",
        "model": row.get("model") or "unknown",
        "input_tokens": int(row.get("prompt_tokens") or 0),
        "cached_tokens": int(row.get("cache_read_input_tokens") or 0),
        "output_tokens": int(row.get("completion_tokens") or 0),
        "cost": float(row.get("spend") or 0),
        "gateway_request_id": row.get("request_id") or row.get("id"),
        "created_at": row.get("startTime") or utc_now().isoformat(),
    }


async def sync_spend_logs(*, start: str, end: str, gateway: Gateway | None = None) -> int:
    """拉取网关流水写入 `llm_usage`；按 request_id 去重。返回新增行数。"""
    from ao.db import tx

    gateway = gateway or current_gateway()
    rows = await gateway.spend_logs(start=start, end=end)
    inserted = 0
    with tx() as conn:
        for raw in rows:
            item = normalize_spend(raw)
            if not item["run_id"] or not item["gateway_request_id"]:
                continue
            result = conn.execute(
                "INSERT INTO llm_usage (run_id, node_id, attempt, purpose, model,"
                " input_tokens, cached_tokens, output_tokens, cost, gateway_request_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT DO NOTHING",
                (
                    item["run_id"],
                    item["node_id"],
                    item["attempt"],
                    item["purpose"],
                    item["model"],
                    item["input_tokens"],
                    item["cached_tokens"],
                    item["output_tokens"],
                    item["cost"],
                    item["gateway_request_id"],
                ),
            )
            inserted += result.rowcount
    return inserted
