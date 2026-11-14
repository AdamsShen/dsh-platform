"""网关适配层的契约测试：字段名按真实 LiteLLM 响应钉住。

2026-11-14 对 litellm:main-latest 实测结论（scripts/probe_gateway.py 可复跑）：
- `/key/generate` 的 `key_name` 是 `sk-...d4jw` 脱敏名，`token` 才是可用于归因的 hash；
- `/spend/logs` 已变成按天聚合，逐条明细在 `/spend/logs/v2`（分页，`{data, total_pages}`）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ao.gateway import LiteLLMGateway, normalize_spend


def _gateway(handler) -> LiteLLMGateway:  # type: ignore[no-untyped-def]
    return LiteLLMGateway(
        base_url="http://gw.test",
        master_key="sk-master",
        transport=httpx.MockTransport(handler),
    )


async def test_issue_key_uses_token_not_display_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/key/generate"
        return httpx.Response(
            200,
            json={
                "key": "sk-real",
                "token": "a" * 64,
                "key_name": "sk-...d4jw",
                "expires": "2026-11-15T03:38:48Z",
            },
        )

    key = await _gateway(handler).issue_key(
        run_id="r1", node_id="n1", attempt=0, purpose="node", budget_usd=1.0
    )
    assert key.key == "sk-real"
    assert key.key_id == "a" * 64
    assert key.expires_at == "2026-11-15T03:38:48Z"


async def test_spend_logs_reads_v2_and_paginates() -> None:
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/spend/logs/v2"
        page = int(request.url.params["page"])
        seen_pages.append(page)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "request_id": f"req-{page}",
                        "api_key": "b" * 64,
                        "model": "gpt-4o-mini",
                        "spend": 0.001,
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "startTime": "2026-11-14T03:38:48Z",
                    }
                ],
                "total_pages": 2,
            },
        )

    rows = await _gateway(handler).spend_logs(start="2026-11-01", end="2026-11-30")
    assert seen_pages == [1, 2]
    assert [row["request_id"] for row in rows] == ["req-1", "req-2"]


async def test_spend_logs_raises_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(Exception, match="网关返回 500"):
        await _gateway(handler).spend_logs(start="2026-11-01", end="2026-11-30")


def test_normalize_spend_exposes_key_token() -> None:
    row = json.loads(
        json.dumps(
            {
                "request_id": "req-1",
                "api_key": "c" * 64,
                "model": "gpt-4o-mini",
                "spend": 0.002,
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "startTime": "2026-11-14T03:38:48Z",
                "metadata": {"user_api_key": "x"},
            }
        )
    )
    mapped = normalize_spend(row)
    assert mapped["key_token"] == "c" * 64
    assert mapped["gateway_request_id"] == "req-1"
    assert mapped["input_tokens"] == 3 and mapped["output_tokens"] == 4
    assert mapped["cost"] == 0.002
    # 未透传归因头时 run 维度为空，只能靠 key_token 反查
    assert mapped["run_id"] is None


def test_normalize_spend_reads_nested_attribution() -> None:
    """归因块在 metadata.spend_logs_metadata 里，不是 metadata 顶层。"""
    row = {
        "request_id": "req-2",
        "api_key": "d" * 64,
        "model": "gpt-4o-mini",
        "spend": 0.001,
        "metadata": {
            "spend_logs_metadata": {
                "run_id": "r1",
                "node_id": "n1",
                "attempt": 2,
                "purpose": "node",
            }
        },
    }
    mapped = normalize_spend(row)
    assert mapped["run_id"] == "r1"
    assert mapped["node_id"] == "n1"
    assert mapped["attempt"] == 2
    assert mapped["purpose"] == "node"
