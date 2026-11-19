"""网关流水归因与同步。

2026-11-14 结论：DSH 走 SDK 子进程，无法保证透传 `x-litellm-spend-logs-metadata`，
所以归因兜底是「每 turn 签虚拟 key → 用 SpendLogs.api_key 反查 gateway_keys」。
这些用例锁住兜底路径，避免只测 header 归因而在 DSH 上失效。
"""

from __future__ import annotations

from typing import Any

from ao.db import fetch_all, tx
from ao.gateway import (
    VirtualKey,
    gateway_openai_base,
    gateway_root,
    normalize_spend,
    sync_spend_logs,
)

TOKEN = "c" * 64


class FakeGateway:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    async def issue_key(self, **_: Any) -> VirtualKey:  # pragma: no cover - 未使用
        return VirtualKey(key="sk-x", key_id=TOKEN)

    async def revoke_key(self, key: str) -> None:  # pragma: no cover - 未使用
        return None

    async def spend_logs(self, *, start: str, end: str) -> list[dict[str, Any]]:
        self.calls += 1
        return self.rows


def _spend_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "request_id": "req-1",
        "api_key": TOKEN,
        "model": "deepseek-v4-flash",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "spend": 0.0001,
        "metadata": {},
        "startTime": "2026-11-14 10:00:00",
    }
    row.update(overrides)
    return row


def _bind(token: str = TOKEN, run_id: str = "run-1") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO gateway_keys (key_token, key_alias, run_id, node_id, attempt,"
            " purpose, budget_usd) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (token, "alias", run_id, "fetch", 0, "node", 1.0),
        )


def test_normalize_spend_without_metadata_keeps_key_token() -> None:
    item = normalize_spend(_spend_row())
    assert item["run_id"] is None
    assert item["key_token"] == TOKEN
    assert item["gateway_request_id"] == "req-1"
    assert item["cost"] == 0.0001


def test_normalize_spend_prefers_request_metadata() -> None:
    row = _spend_row(
        metadata={
            "spend_logs_metadata": {
                "run_id": "run-hdr",
                "node_id": "n2",
                "attempt": 1,
                "purpose": "node",
            }
        }
    )
    item = normalize_spend(row)
    # 请求级 header 优先于 token 反查：反查只在缺失时兜底
    assert item["run_id"] == "run-hdr"
    assert item["node_id"] == "n2"
    assert item["attempt"] == 1


async def test_sync_backfills_attribution_from_key_token() -> None:
    _bind()
    inserted = await sync_spend_logs(
        start="2026-11-14", end="2026-11-14", gateway=FakeGateway([_spend_row()])
    )
    assert inserted == 1

    rows = fetch_all(
        "SELECT run_id, node_id, attempt, purpose, gateway_request_id FROM llm_usage"
    )
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["node_id"] == "fetch"
    assert rows[0]["attempt"] == 0
    assert rows[0]["gateway_request_id"] == "req-1"


async def test_sync_is_idempotent_on_same_request() -> None:
    _bind()
    gateway = FakeGateway([_spend_row()])
    first = await sync_spend_logs(start="2026-11-14", end="2026-11-14", gateway=gateway)
    second = await sync_spend_logs(start="2026-11-14", end="2026-11-14", gateway=gateway)
    assert (first, second) == (1, 0)
    assert len(fetch_all("SELECT id FROM llm_usage")) == 1


async def test_sync_drops_rows_without_run_or_request() -> None:
    # 没签过 key、也没带归因头：无法归属，宁可丢弃也不写脏账
    inserted = await sync_spend_logs(
        start="2026-11-14", end="2026-11-14", gateway=FakeGateway([_spend_row(api_key=None)])
    )
    assert inserted == 0
    assert fetch_all("SELECT id FROM llm_usage") == []


def test_harness_recognizes_budget_422() -> None:
    """LiteLLM 超预算实测返回 422（不是 402），必须识别成 budget 而不是普通失败。"""
    from ao.harness._dsh_runtime import _is_budget_error

    assert _is_budget_error({"type": "budget_exceeded", "message": "Budget has been exceeded!"})
    assert _is_budget_error({"message": "Budget has been exceeded! Key=abc"})
    assert not _is_budget_error({"message": "invalid model name"})
    assert not _is_budget_error(None)


async def test_issue_key_maps_budget_422() -> None:
    import httpx

    from ao.gateway import BudgetExceeded, LiteLLMGateway

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"error": {"type": "budget_exceeded", "message": "Budget has been exceeded!"}}
        )

    gateway = LiteLLMGateway(
        base_url="http://gw.test", master_key="sk-m", transport=httpx.MockTransport(handler)
    )
    try:
        await gateway.issue_key(
            run_id="r", node_id="n", attempt=0, purpose="node", budget_usd=0.0
        )
    except BudgetExceeded:
        return
    raise AssertionError("422 budget 必须映射成 BudgetExceeded")


def test_gateway_endpoints(monkeypatch: Any) -> None:
    from ao.settings import settings

    monkeypatch.setattr(settings, "gateway_url", "http://gw:4000")
    # DSH 的 base_url 必须是根：SDK 自己拼 /v1/messages
    assert gateway_root() == "http://gw:4000"
    assert gateway_openai_base() == "http://gw:4000/v1"
    monkeypatch.setattr(settings, "gateway_url", "http://gw:4000/")
    assert gateway_root() == "http://gw:4000"
    monkeypatch.setattr(settings, "gateway_url", "")
    assert gateway_root() == ""
    assert gateway_openai_base() == ""
