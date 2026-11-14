"""LiteLLM 网关：虚拟 key + spend log。

账单真值只在网关：
- 每次 `(run, node, attempt, purpose)` 前签一个虚拟 key，`attempt` 终态吊销；
- `llm_usage` 定时从 `/spend/logs` 拉取，按 `gateway_request_id` 去重；
- `runs.spent_usd` 由 `llm_usage` 聚合刷新，不做增量累加。

HTTP 路径按 LiteLLM Proxy 的公开接口写；接口若有差异，改这里一处。
"""

from __future__ import annotations

import logging
import uuid
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
        # `key_alias` 全局唯一且删除后仍占位（实测事实 3），固定 alias 会让重试直接 400，
        # 所以拼一个短后缀；归因不靠 alias，靠返回的 token 查 gateway_keys。
        alias = f"{run_id}:{node_id}:{attempt}:{purpose}:{uuid.uuid4().hex[:8]}"
        payload = {
            "key_alias": alias,
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
        # 签发侧预算拒绝是 402；调用侧超预算是 422（实测，type=budget_exceeded），
        # 两种都要当预算问题处理，不能退化成可重试的网关错误。
        if resp.status_code == 402 or (
            resp.status_code == 422 and "budget" in resp.text.lower()
        ):
            raise BudgetExceeded(resp.status_code, resp.text[:300])
        if resp.status_code >= 400:
            raise GatewayError(resp.status_code, resp.text[:300])
        data = resp.json()
        # LiteLLM 返回的 `key_name` 是 `sk-...d4jw` 这种脱敏展示名，不能当标识；
        # `token` 才是 key 的 hash，也等于 SpendLogs 里的 `api_key`，归因只能靠它。
        key_id = data.get("token") or data.get("key_name") or ""
        return VirtualKey(
            key=data.get("key", ""),
            key_id=str(key_id),
            expires_at=data.get("expires"),
        )

    async def revoke_key(self, key: str) -> None:
        async with self._client() as client:
            resp = await client.post("/key/delete", json={"keys": [key]})
        if resp.status_code >= 400:
            logger.warning("虚拟 key 吊销失败（保留支出即可，不阻塞收尾）: %s", resp.text[:200])

    async def spend_logs(self, *, start: str, end: str) -> list[dict[str, Any]]:
        """拉逐条流水（不是每日汇总）。

        LiteLLM 的 `/spend/logs` 现在返回按天聚合（`{spend, startTime, users, models}`），
        没有 `request_id`/`usage`，直接据此归因会全部丢弃；明细在 `/spend/logs/v2`，
        分页返回 `{data, page, total_pages}`。日期参数只接受 `YYYY-MM-DD`，带时分秒会 500。
        注意 `end_date` 按当天 00:00 解析：要包含当天的流水必须传「明天」。
        """
        rows: list[dict[str, Any]] = []
        page = 1
        async with self._client() as client:
            while True:
                resp = await client.get(
                    "/spend/logs/v2",
                    params={
                        "start_date": start,
                        "end_date": end,
                        "page": page,
                        "page_size": 500,
                    },
                )
                if resp.status_code >= 400:
                    raise GatewayError(resp.status_code, resp.text[:300])
                payload = resp.json()
                data = payload.get("data") if isinstance(payload, dict) else payload
                if not isinstance(data, list) or not data:
                    break
                rows.extend(data)
                total_pages = payload.get("total_pages") if isinstance(payload, dict) else None
                if page >= int(total_pages or 1):
                    break
                page += 1
        return rows


def current_gateway() -> Gateway:
    if settings.mock:
        return NullGateway()
    if not settings.gateway_url or not settings.gateway_master_key:
        # 不静默降级：真跑必须有网关，否则账单不可信
        raise RuntimeError("AO_MOCK=0 但 AO_GATEWAY_URL / AO_GATEWAY_MASTER_KEY 未配置")
    return LiteLLMGateway()


def gateway_root() -> str:
    """网关根地址。

    DSH 走 Anthropic Messages 协议（`POST /v1/messages`，实测），SDK 自己拼 `/v1`，
    所以传给 harness 的 `base_url` 必须是根，不能带 `/v1`（否则变成 `/v1/v1/messages`）。
    """
    return (settings.gateway_url or "").rstrip("/")


def gateway_openai_base() -> str:
    """OpenAI 兼容端点（`/v1/chat/completions`），探针与 OpenAI SDK 用这个。"""
    root = gateway_root()
    return f"{root}/v1" if root and not root.endswith("/v1") else root


def bind_key(
    key: VirtualKey,
    *,
    run_id: str,
    node_id: str,
    attempt: int,
    purpose: str,
    budget_usd: float,
) -> None:
    """登记 token → 归因。签完 key 立刻写，进程崩了也能靠 token 反查。"""
    from ao.db import tx

    with tx() as conn:
        conn.execute(
            "INSERT INTO gateway_keys (key_token, key_alias, run_id, node_id, attempt,"
            " purpose, budget_usd, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (key_token) DO NOTHING",
            (
                key.key_id,
                f"{run_id}:{node_id}:{attempt}:{purpose}",
                run_id,
                node_id,
                attempt,
                purpose,
                budget_usd,
                key.expires_at,
            ),
        )


def mark_revoked(key_token: str) -> None:
    from ao.db import tx

    with tx() as conn:
        conn.execute(
            "UPDATE gateway_keys SET revoked_at = now() WHERE key_token = %s",
            (key_token,),
        )


def normalize_spend(row: dict[str, Any]) -> dict[str, Any]:
    """把网关流水映射成 `llm_usage` 需要的形状。

    归因信息在 `metadata.spend_logs_metadata`（比 `metadata` 深一层），来源是调用方
    请求体里的 `metadata.spend_logs_metadata` 或请求头 `x-litellm-spend-logs-metadata`
    （2026-11-14 实测，见 docs/reports/2026-11-14-gateway-probe.md）。
    签发 key 时写在 key 上的 metadata **不会**进流水，所以不能用它做 run 归因。

    兜底：`key_token`（= SpendLogs.api_key，即 `/key/generate` 返回的 `token`）始终存在，
    调用方在拿不到 `spend_logs_metadata` 时可以凭它反查本地签发表。
    """
    metadata = row.get("metadata") or {}
    # 请求级归因块；老版本或未透传时为空 dict
    attribution = metadata.get("spend_logs_metadata") or {}
    return {
        "key_token": row.get("api_key"),
        "run_id": attribution.get("run_id") or metadata.get("run_id") or row.get("run_id"),
        "node_id": attribution.get("node_id") or metadata.get("node_id"),
        "attempt": int(attribution.get("attempt") or metadata.get("attempt") or 0),
        "purpose": attribution.get("purpose") or metadata.get("purpose") or "node",
        "model": row.get("model") or "unknown",
        "input_tokens": int(row.get("prompt_tokens") or 0),
        "cached_tokens": int(row.get("cache_read_input_tokens") or 0),
        "output_tokens": int(row.get("completion_tokens") or 0),
        "cost": float(row.get("spend") or 0),
        "gateway_request_id": row.get("request_id") or row.get("id"),
        "created_at": row.get("startTime") or utc_now().isoformat(),
    }


async def sync_spend_logs(*, start: str, end: str, gateway: Gateway | None = None) -> int:
    """拉取网关流水写入 `llm_usage`；按 request_id 去重，并刷新受影响 run 的 spent_usd。

    归因优先级：`metadata.spend_logs_metadata`（请求头透传，最准）→ 本地 `gateway_keys`
    按 `api_key` 反查。后者是 DSH 路径的兜底：harness 不能保证透传自定义头，
    只能靠每 turn 签的虚拟 key 认领流水。
    """
    from ao.db import tx

    gateway = gateway or current_gateway()
    rows = await gateway.spend_logs(start=start, end=end)
    inserted = 0
    touched: set[str] = set()
    with tx() as conn:
        # 未知 token 一次批量反查，避免逐行打库
        tokens = [str(raw.get("api_key") or "") for raw in rows]
        tokens = [t for t in dict.fromkeys(tokens) if t]
        bindings: dict[str, dict[str, Any]] = {}
        if tokens:
            found = conn.execute(
                "SELECT key_token, run_id, node_id, attempt, purpose FROM gateway_keys"
                " WHERE key_token = ANY(%s)",
                (tokens,),
            ).fetchall()
            bindings = {row["key_token"]: row for row in found}

        for raw in rows:
            item = normalize_spend(raw)
            if not item["run_id"] and item["key_token"] in bindings:
                bound = bindings[item["key_token"]]
                item["run_id"] = bound["run_id"]
                item["node_id"] = bound["node_id"]
                item["attempt"] = bound["attempt"]
                item["purpose"] = bound["purpose"]
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
            touched.add(item["run_id"])

        for run_id in touched:
            conn.execute(
                "UPDATE runs SET spent_usd = COALESCE("
                "  (SELECT SUM(cost) FROM llm_usage WHERE run_id = %s), 0) WHERE id = %s",
                (run_id, run_id),
            )
    return inserted
