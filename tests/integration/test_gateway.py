from __future__ import annotations

import httpx
import pytest

from ao.db import fetch_all, tx
from ao.gateway import BudgetExceeded, LiteLLMGateway, sync_spend_logs


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.integration
async def test_issue_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/key/generate"
        return httpx.Response(200, json={"key": "sk-abc", "key_name": "k1"})

    gateway = LiteLLMGateway("http://gw", "master", transport=transport(handler))
    key = await gateway.issue_key(
        run_id="r1", node_id="n1", attempt=0, purpose="node", budget_usd=1.0
    )
    assert key.key == "sk-abc"
    assert key.key_id == "k1"


@pytest.mark.integration
async def test_402_becomes_budget_exceeded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="budget exceeded")

    gateway = LiteLLMGateway("http://gw", "master", transport=transport(handler))
    with pytest.raises(BudgetExceeded):
        await gateway.issue_key(
            run_id="r1", node_id="n1", attempt=0, purpose="node", budget_usd=0
        )


@pytest.mark.integration
async def test_sync_spend_logs_dedupes_by_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "request_id": "req-1",
                    "model": "deepseek-chat",
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "spend": 0.001,
                    "metadata": {"run_id": "run_x", "node_id": "draft", "attempt": 0},
                }
            ],
        )

    gateway = LiteLLMGateway("http://gw", "master", transport=transport(handler))
    # runs 有外键指向 workflow_defs，先插 workflow 再插 run
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES ('default','default') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO workflow_defs (id, tenant_id, name, created_by)"
            " VALUES ('wf_x','default','wf','test') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO runs (id, tenant_id, workflow_id, version, package_ref, dedupe_key,"
            " status) VALUES ('run_x','default','wf_x',1,'/tmp','dk_x','running')"
            " ON CONFLICT (id) DO NOTHING"
        )

    assert await sync_spend_logs(start="2026-01-01", end="2026-01-02", gateway=gateway) == 1
    assert await sync_spend_logs(start="2026-01-01", end="2026-01-02", gateway=gateway) == 0

    rows = fetch_all("SELECT * FROM llm_usage WHERE run_id = 'run_x'")
    assert len(rows) == 1
    assert rows[0]["gateway_request_id"] == "req-1"
    assert float(rows[0]["cost"]) == pytest.approx(0.001)
